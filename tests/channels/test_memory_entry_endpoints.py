"""HTTP endpoint tests for the P12 entry browse / forget / backlinks routes.

Uses the unified Starlette ASGI app (``build_gateway_http_app``) via
``TestClient`` instead of spawning a real WebSocketChannel socket.
Smoke-level — the deep behaviour is covered by
``test_graph_api_entries.py`` (calling the helpers directly).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from durin.api.asgi import build_gateway_http_app
from durin.channels.websocket import WebSocketChannel


def _seed_entry(
    ws: Path,
    *,
    class_name: str,
    entry_id: str,
    body: str = "obs",
    entities: tuple[str, ...] = ("person:alice",),
) -> Path:
    ent_lines = (
        "entities:\n" + "".join(f"  - {e}\n" for e in entities)
        if entities else ""
    )
    fm = (
        f"id: {entry_id}\n"
        f"headline: {entry_id} headline\n"
        f"valid_from: 2026-05-30\n"
        f"{ent_lines}"
    )
    p = ws / "memory" / class_name / f"{entry_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return p


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bus: MagicMock):
    """Unified ASGI test client with isolated data dir and workspace.

    Memory is disabled so the vector-cleanup branch in forget_entry is a
    no-op without needing fastembed.
    """
    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    fake_cfg = SimpleNamespace(
        workspace_path=tmp_path,
        memory=SimpleNamespace(
            enabled=False,
            embedding=SimpleNamespace(model=""),
        ),
    )

    spa = tmp_path / "dist"
    spa.mkdir()
    (spa / "index.html").write_text(
        "<!doctype html><title>durin</title><div id=root></div>", encoding="utf-8"
    )

    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }

    with patch("durin.config.loader.load_config", return_value=fake_cfg):
        channel = WebSocketChannel(cfg, bus)
        registry = channel._services
        auth = registry.get("auth")
        app = build_gateway_http_app(channel, registry, auth=auth, static_dist_path=spa)
        yield TestClient(app)


def _token(client: TestClient) -> str:
    r = client.get("/webui/bootstrap")
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_memory_entry_routes_require_bearer(
    client: TestClient,
) -> None:
    """All 3 new routes return 401 without a valid token."""
    for path in (
        "/api/memory/entry?uri=memory/episodic/x",
        "/api/memory/forget?uri=memory/episodic/x",
        "/api/memory/backlinks?uri=memory/episodic/x",
    ):
        r = client.get(path)
        assert r.status_code == 401, f"expected 401 for {path}, got {r.status_code}"


def test_memory_entry_endpoint_returns_payload(
    tmp_path: Path, client: TestClient,
) -> None:
    _seed_entry(
        tmp_path, class_name="episodic", entry_id="obs-1",
        body="Alice loves rust",
    )
    tok = _token(client)
    auth = {"Authorization": f"Bearer {tok}"}

    r = client.get(
        "/api/memory/entry?uri=memory/episodic/obs-1",
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["uri"] == "memory/episodic/obs-1"
    assert body["class_name"] == "episodic"
    assert "Alice loves rust" in body["body"]

    # 404 when the entry doesn't exist.
    r404 = client.get(
        "/api/memory/entry?uri=memory/episodic/ghost",
        headers=auth,
    )
    assert r404.status_code == 404


def test_memory_forget_endpoint_archives_and_protects(
    tmp_path: Path, client: TestClient,
) -> None:
    _seed_entry(tmp_path, class_name="episodic", entry_id="obs-2")
    auth = {"Authorization": f"Bearer {_token(client)}"}

    # Happy path: archive an existing entry.
    r = client.get(
        "/api/memory/forget?uri=memory/episodic/obs-2",
        headers=auth,
    )
    assert r.status_code == 200
    assert r.json() == {"result": "archived"}
    assert not (tmp_path / "memory" / "episodic" / "obs-2.md").exists()
    assert (tmp_path / "memory" / "archive" / "episodic" / "obs-2.md").exists()

    # Protected: entity URIs return 403.
    r_protected = client.get(
        "/api/memory/forget?uri=memory/entities/person/marcelo",
        headers=auth,
    )
    assert r_protected.status_code == 403
    assert r_protected.json()["result"] == "protected"

    # Invalid URI returns 400.
    r_bad = client.get(
        "/api/memory/forget?uri=garbage",
        headers=auth,
    )
    assert r_bad.status_code == 400
    assert r_bad.json()["result"] == "invalid"


def test_memory_backlinks_endpoint(
    tmp_path: Path, client: TestClient,
) -> None:
    _seed_entry(tmp_path, class_name="episodic", entry_id="target")
    _seed_entry(
        tmp_path, class_name="episodic", entry_id="ref",
        body="see [[memory/episodic/target]] for more",
        entities=("person:bob",),
    )
    auth = {"Authorization": f"Bearer {_token(client)}"}

    r = client.get(
        "/api/memory/backlinks?uri=memory/episodic/target",
        headers=auth,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["uri"] == "memory/episodic/target"
    assert len(body["backlinks"]) == 1
    assert body["backlinks"][0]["uri"] == "memory/episodic/ref"
    assert body["truncated"] is False
