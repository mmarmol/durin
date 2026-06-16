"""End-to-end tests for the embedded webui's HTTP routes on the WebSocket channel."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_gateway_http_app
from durin.channels.websocket import WebSocketChannel
from durin.session.manager import Session, SessionManager


def _ch(
    bus: Any,
    *,
    session_manager: SessionManager | None = None,
    static_dist_path: Path | None = None,
    runtime_model_name: Any | None = None,
    **extra: Any,
) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    cfg.update(extra)
    ws_kwargs: dict[str, Any] = {
        "session_manager": session_manager,
        "static_dist_path": static_dist_path,
    }
    if runtime_model_name is not None:
        ws_kwargs["runtime_model_name"] = runtime_model_name
    return WebSocketChannel(
        cfg,
        bus,
        **ws_kwargs,
    )


def _make_client(
    bus: Any,
    *,
    session_manager: SessionManager | None = None,
    static_dist_path: Path | None = None,
    runtime_model_name: Any | None = None,
    **extra: Any,
) -> TestClient:
    channel = _ch(
        bus,
        session_manager=session_manager,
        static_dist_path=static_dist_path,
        runtime_model_name=runtime_model_name,
        **extra,
    )
    registry = channel._services
    app = build_gateway_http_app(
        channel, registry, auth=registry.get("auth"), static_dist_path=static_dist_path
    )
    return TestClient(app)


def _token(client: TestClient) -> str:
    r = client.get("/webui/bootstrap")
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


def _seed_session(workspace: Path, key: str = "websocket:test") -> SessionManager:
    sm = SessionManager(workspace)
    s = Session(key=key)
    s.add_message("user", "hi")
    s.add_message("assistant", "hello back")
    sm.save(s)
    return sm


def _seed_many(workspace: Path, keys: list[str]) -> SessionManager:
    sm = SessionManager(workspace)
    for k in keys:
        s = Session(key=k)
        s.add_message("user", f"hi from {k}")
        sm.save(s)
    return sm


def test_bootstrap_returns_token_for_localhost(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path)
    client = _make_client(bus, session_manager=sm)
    resp = client.get("/webui/bootstrap")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"].startswith("nbwt_")
    assert body["ws_path"] == "/"
    assert body["expires_in"] > 0
    assert isinstance(body.get("model_name"), str)


def test_sessions_routes_require_bearer_token(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:abc")
    client = _make_client(bus, session_manager=sm)

    # Unauthenticated → 401.
    deny = client.get("/api/sessions")
    assert deny.status_code == 401

    # Mint a token via bootstrap, then call the API with it.
    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    listing = client.get("/api/sessions", headers=auth)
    assert listing.status_code == 200
    keys = [s["key"] for s in listing.json()["sessions"]]
    assert "websocket:abc" in keys
    # Server stays an opaque source: filesystem paths must not leak to the wire.
    assert all("path" not in s for s in listing.json()["sessions"])

    msgs = client.get("/api/sessions/websocket:abc/messages", headers=auth)
    assert msgs.status_code == 200
    body = msgs.json()
    assert body["key"] == "websocket:abc"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_sessions_list_only_returns_websocket_sessions_by_default(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    # Seed a realistic multi-channel disk state: CLI, Slack, Lark and
    # websocket sessions all live in the same ``sessions/`` directory.
    sm = _seed_many(
        tmp_path,
        [
            "cli:direct",
            "slack:C123",
            "lark:oc_abc",
            "websocket:alpha",
            "websocket:beta",
        ],
    )
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    listing = client.get("/api/sessions", headers=auth)
    assert listing.status_code == 200
    keys = {s["key"] for s in listing.json()["sessions"]}
    # Only websocket-channel sessions are part of the webui surface; CLI /
    # Slack / Lark rows would be non-resumable from the browser.
    assert keys == {"websocket:alpha", "websocket:beta"}


def test_session_delete_removes_file(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:doomed")
    from durin.utils.webui_transcript import append_transcript_object

    append_transcript_object("websocket:doomed", {"event": "user", "chat_id": "doomed", "text": "x"})
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    path = sm._get_session_path("websocket:doomed")
    assert path.exists()
    webui_path = tmp_path / "webui" / f"{SessionManager.safe_key('websocket:doomed')}.jsonl"
    assert webui_path.is_file()
    resp = client.get("/api/sessions/websocket:doomed/delete", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert not path.exists()
    assert not webui_path.exists()


def test_session_routes_accept_percent_encoded_websocket_keys(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:encoded-key")
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    msgs = client.get("/api/sessions/websocket%3Aencoded-key/messages", headers=auth)
    assert msgs.status_code == 200
    assert msgs.json()["key"] == "websocket:encoded-key"

    path = sm._get_session_path("websocket:encoded-key")
    assert path.exists()
    deleted = client.get(
        "/api/sessions/websocket%3Aencoded-key/delete", headers=auth
    )
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert not path.exists()


def test_session_routes_reject_non_websocket_keys(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_many(
        tmp_path,
        [
            "websocket:kept",
            "cli:direct",
            "slack:C123",
        ],
    )
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    # The webui list already hides non-websocket sessions; handcrafted URLs
    # should hit the same boundary rather than exposing or deleting them.
    msgs = client.get("/api/sessions/cli:direct/messages", headers=auth)
    assert msgs.status_code == 404

    doomed = sm._get_session_path("slack:C123")
    assert doomed.exists()
    deny_delete = client.get(
        "/api/sessions/slack:C123/delete", headers=auth
    )
    assert deny_delete.status_code == 404
    assert doomed.exists()


def test_rename_during_active_turn_preserves_title_and_messages(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B2: a rename concurrent with an active turn must not lose-update.

    The agent loop holds the cached Session and appends an in-flight
    (unsaved) message; the user renames mid-turn. With the old `_load`
    path the rename mutated a separate object, so the loop's end-of-turn
    save clobbered the title. Sharing the cached instance keeps both.
    """
    from durin.utils.webui_titles import WEBUI_TITLE_METADATA_KEY

    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:test")
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    # Loop obtains the cached session and appends an unsaved message.
    cached = sm.get_or_create("websocket:test")
    cached.add_message("user", "in-flight-msg")

    resp = client.get(
        "/api/sessions/websocket:test/rename?title=Renamed",
        headers=auth,
    )
    assert resp.status_code == 200

    # Loop finishes the turn and saves its (cached) session.
    sm.save(cached)

    # Disk truth (fresh manager, no cache): both must survive.
    fresh = SessionManager(tmp_path)._load("websocket:test")
    assert fresh is not None
    assert fresh.metadata.get(WEBUI_TITLE_METADATA_KEY) == "Renamed"
    assert any(m.get("content") == "in-flight-msg" for m in fresh.messages)


def test_delete_during_active_turn_is_not_resurrected(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adjacent to B2: deleting a session while a turn holds it cached
    must stay deleted — the loop's end-of-turn save must not resurrect
    the file."""
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path, key="websocket:doomed")
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    cached = sm.get_or_create("websocket:doomed")
    cached.add_message("user", "mid-turn")

    resp = client.get(
        "/api/sessions/websocket:doomed/delete",
        headers=auth,
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # Loop finishes and tries to save its now-deleted cached session.
    sm.save(cached)

    assert not sm._get_session_path("websocket:doomed").exists()


def test_session_routes_reject_invalid_key(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    sm = _seed_session(tmp_path)
    client = _make_client(bus, session_manager=sm)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    # Invalid characters in the key -> regex match fails -> 404
    # (route doesn't match, falls through to channel 404).
    resp = client.get("/api/sessions/bad%20key/messages", headers=auth)
    assert resp.status_code in {400, 404}


def test_static_serves_index_when_dist_present(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>nbweb</title>")
    (dist / "favicon.svg").write_text("<svg/>")
    sm = _seed_session(tmp_path / "ws_state")
    client = _make_client(bus, session_manager=sm, static_dist_path=dist)

    # Bare ``GET /`` is a browser opening the app: it must return the SPA
    # index.html, not the WS-upgrade handler's 401/426.
    root = client.get("/")
    assert root.status_code == 200
    assert "nbweb" in root.text
    asset = client.get("/favicon.svg")
    assert asset.status_code == 200
    assert "<svg" in asset.text
    # Unknown SPA route falls back to index.html.
    spa = client.get("/sessions/abc")
    assert spa.status_code == 200
    assert "nbweb" in spa.text


def test_static_rejects_path_traversal(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("ok")
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")
    client = _make_client(bus, static_dist_path=dist)
    resp = client.get("/../secret.txt")
    # Normalized by httpx into /secret.txt → falls back to index.html, not 'classified'.
    assert "classified" not in resp.text


def _seed_quarantine(workspace: Path, name: str = "pending") -> None:
    q = workspace / ".durin" / "import-quarantine" / name
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nhi\n")
    (q / ".scan.json").write_text(
        json.dumps({"source": "github:x/y", "verdict": "caution", "findings": []})
    )


def test_skills_quarantine_route_not_shadowed_by_skill_name(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`GET /api/skills/quarantine` must hit the quarantine handler, not the
    `/api/skills/<name>` skill-get route with name='quarantine'."""
    from types import SimpleNamespace

    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_quarantine(ws)
    monkeypatch.setattr(
        "durin.config.loader.load_config",
        lambda *a, **k: SimpleNamespace(workspace_path=ws),
    )
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    client = _make_client(bus)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    resp = client.get("/api/skills/quarantine", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    # Routed to the quarantine handler: payload has the quarantine list,
    # NOT a skill-get shape (which would 404 "skill not found: quarantine"
    # or return a {name, mode, content} body for a skill named quarantine).
    assert "quarantined" in body
    assert "content" not in body
    names = {s["name"] for s in body["quarantined"]}
    assert "pending" in names


# --- §6.B import routes (local source, no network) ---------------------------

def _mk_source_skill(root: Path, name: str = "imported", body: str = "Step 1: do it.\n") -> Path:
    d = root / "src" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n{body}")
    return d


def _real_cfg_at(ws: Path):
    from durin.config.loader import load_config
    cfg = load_config()
    cfg.agents.defaults.workspace = str(ws)
    # judge trigger defaults to "off" → no LLM call in tests (hermetic)
    return cfg


def test_skills_resolve_route_lists_local_candidates(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import quote
    ws = tmp_path / "ws"
    ws.mkdir()
    src = _mk_source_skill(tmp_path)
    cfg = _real_cfg_at(ws)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    client = _make_client(bus)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}
    resp = client.get(
        f"/api/skills/resolve?source={quote(str(src))}", headers=auth
    )
    assert resp.status_code == 200
    body = resp.json()
    # routed to resolve (not skill-get with name='resolve')
    assert "candidates" in body
    assert "imported" in {c["name"] for c in body["candidates"]}


def test_skills_import_then_approve_installs(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import quote
    ws = tmp_path / "ws"
    ws.mkdir()
    src = _mk_source_skill(tmp_path)
    cfg = _real_cfg_at(ws)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    client = _make_client(bus)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}
    imp = client.get(
        f"/api/skills/import?source={quote(str(src))}", headers=auth
    )
    assert imp.status_code == 200
    body = imp.json()
    assert body["quarantined"] == "imported"
    assert body["verdict"] == "safe"
    # a safe but out-of-allowlist skill needs confirm: bare approve is refused.
    refused = client.get("/api/skills/imported/approve", headers=auth)
    assert refused.status_code == 409
    assert refused.json()["refused"] == "confirm"
    ok = client.get("/api/skills/imported/approve?confirm=true", headers=auth)
    assert ok.status_code == 200 and ok.json()["ok"]
    assert (ws / "skills" / "imported" / "SKILL.md").is_file()


def test_skill_judge_route_runs_on_demand(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    q = ws / ".durin" / "import-quarantine" / "cand"
    q.mkdir(parents=True)
    (q / "SKILL.md").write_text("---\nname: cand\ndescription: d\n---\nbe helpful\n")
    (q / ".scan.json").write_text(json.dumps({"source": "github:o/r", "verdict": "safe", "findings": []}))
    cfg = _real_cfg_at(ws)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(
        "durin.memory.llm_invoke.default_llm_invoke",
        lambda prompt, *, model=None: "===FINDINGS===\ncaution | intent | SKILL.md | reads an API key quietly\n===END===\n")
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    client = _make_client(bus)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}
    resp = client.get("/api/skills/cand/judge", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["judged"] is True
    assert body["verdict"] == "caution"
    assert any(f["category"].startswith("llm:") for f in body["findings"])


def test_github_token_test_route_not_shadowed(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `/api/skills/github-token-test` must hit the token-test handler, not
    # `/api/skills/<name>` with name='github-token-test'.
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = _real_cfg_at(ws)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("durin.security.secrets.resolve_secret", lambda ref: "")
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    client = _make_client(bus)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}
    resp = client.get(
        "/api/skills/github-token-test?secret=ghx", headers=auth
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and "content" not in body  # token-test shape, no network


def test_skill_reject_route_removes_quarantine(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.parse import quote
    ws = tmp_path / "ws"
    ws.mkdir()
    src = _mk_source_skill(tmp_path)
    cfg = _real_cfg_at(ws)
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    client = _make_client(bus)

    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}
    client.get(f"/api/skills/import?source={quote(str(src))}", headers=auth)
    assert (ws / ".durin" / "import-quarantine" / "imported").is_dir()
    rej = client.get("/api/skills/imported/reject", headers=auth)
    assert rej.status_code == 200 and rej.json()["ok"]
    assert not (ws / ".durin" / "import-quarantine" / "imported").exists()


def test_unknown_route_returns_404(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: tmp_path)
    client = _make_client(bus)
    resp = client.get("/api/unknown")
    assert resp.status_code == 404


def test_api_token_pool_purges_expired(bus: MagicMock, tmp_path: Path) -> None:
    sm = _seed_session(tmp_path)
    channel = _ch(bus, session_manager=sm)
    # Don't start a server — directly inject and validate.
    import time as _time
    channel._api_tokens["expired"] = _time.monotonic() - 1
    channel._api_tokens["live"] = _time.monotonic() + 60

    class _FakeReq:
        path = "/api/sessions"
        headers = {"Authorization": "Bearer expired"}

    assert channel._check_api_token(_FakeReq()) is False

    class _LiveReq:
        path = "/api/sessions"
        headers = {"Authorization": "Bearer live"}

    assert channel._check_api_token(_LiveReq()) is True


class _FakeConn:
    """Minimal connection stub with a configurable remote_address."""

    def __init__(self, remote_address: tuple[str, int]):
        self.remote_address = remote_address

    def respond(self, status: int, body: str) -> Any:
        from websockets.http11 import Response

        return Response(status=status, body=body.encode())


class _FakeReq:
    """Minimal request stub with configurable headers."""

    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


_REMOTE = _FakeConn(("192.168.1.5", 12345))
_LOCAL = _FakeConn(("127.0.0.1", 12345))
_NO_HEADERS = _FakeReq()


def test_wildcard_host_without_auth_raises_on_startup(bus: MagicMock) -> None:
    import pytest
    from pydantic_core import ValidationError

    with pytest.raises(ValidationError, match="token"):
        _ch(bus, host="0.0.0.0")


def test_wildcard_host_with_token_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", token="my-token")
    assert channel.config.host == "0.0.0.0"


def test_wildcard_host_with_secret_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="s3cret")
    assert channel.config.host == "0.0.0.0"


def test_wildcard_ipv6_without_auth_raises(bus: MagicMock) -> None:
    import pytest
    from pydantic_core import ValidationError

    with pytest.raises(ValidationError, match="token"):
        _ch(bus, host="::")


def test_wildcard_ipv6_with_secret_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="::", tokenIssueSecret="s3cret")
    resp = channel._handle_bootstrap(
        _REMOTE, _FakeReq({"X-Durin-Auth": "s3cret"})
    )
    assert resp.status_code == 200


def test_bootstrap_accepts_static_token_as_secret(bus: MagicMock) -> None:
    """When only token (not token_issue_secret) is set, bootstrap accepts it."""
    channel = _ch(bus, host="0.0.0.0", token="static-tok")
    resp = channel._handle_bootstrap(
        _REMOTE, _FakeReq({"Authorization": "Bearer static-tok"})
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["token"].startswith("nbwt_")


def test_localhost_without_auth_is_valid(bus: MagicMock) -> None:
    channel = _ch(bus, host="127.0.0.1")
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200


def test_bootstrap_prefers_runtime_model_name(bus: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "durin.channels.websocket._default_model_name_from_config",
        lambda: "from-disk",
    )
    channel = _ch(bus, host="127.0.0.1", runtime_model_name=lambda: "  live/model  ")
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "live/model"


def test_bootstrap_falls_back_when_runtime_returns_empty(bus: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "durin.channels.websocket._default_model_name_from_config",
        lambda: "from-disk",
    )
    channel = _ch(bus, host="127.0.0.1", runtime_model_name=lambda: "   ")
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "from-disk"


def test_bootstrap_falls_back_when_runtime_raises(bus: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "durin.channels.websocket._default_model_name_from_config",
        lambda: "from-disk",
    )

    def boom():
        raise RuntimeError("resolver failed")

    channel = _ch(bus, host="127.0.0.1", runtime_model_name=boom)
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["model_name"] == "from-disk"


def test_bootstrap_rejects_wrong_secret(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="correct")
    resp = channel._handle_bootstrap(
        _REMOTE, _FakeReq({"Authorization": "Bearer wrong"})
    )
    assert resp.status_code == 401


def test_bootstrap_accepts_remote_with_valid_secret(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="s3cret")
    resp = channel._handle_bootstrap(
        _REMOTE, _FakeReq({"Authorization": "Bearer s3cret"})
    )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["token"].startswith("nbwt_")


def test_bootstrap_accepts_x_durin_auth_header(bus: MagicMock) -> None:
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="s3cret")
    resp = channel._handle_bootstrap(
        _REMOTE, _FakeReq({"X-Durin-Auth": "s3cret"})
    )
    assert resp.status_code == 200


def test_bootstrap_secret_also_enforced_on_localhost(bus: MagicMock) -> None:
    """When secret is set, even localhost must provide it (reverse-proxy safety)."""
    channel = _ch(bus, host="0.0.0.0", tokenIssueSecret="s3cret")
    resp = channel._handle_bootstrap(_LOCAL, _NO_HEADERS)
    assert resp.status_code == 401
