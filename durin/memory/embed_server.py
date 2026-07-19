"""The standing embedding service (loopback HTTP, gateway-supervised).

Every durin process needs embeddings (memory writes, search, indexing, the
dream), and before this service each PROCESS carried its own copy of the
model — during a dream, the gateway's pool child and the dream worker's pool
child held the same ~0.5-1GB model twice. This module is the single copy:

- ``build_embed_app`` — a minimal Starlette app: ``POST /v1/embeddings``
  (OpenAI-compatible shape, bearer-token auth) + ``GET /health`` (open).
  The fastembed model lives INLINE in this process; the server process is
  the isolation boundary (its supervisor restarts it to reclaim the ONNX
  arena, replacing pool-child recycling).
- ``EmbedResultCache`` — sqlite LRU keyed (model, kind, sha256(text)):
  re-embedding unchanged content costs zero compute.
- Discovery — ``DURIN_HOME/embed-server.json`` with the owner's process
  identity; clients treat a dead-owner file as absent (same liveness
  contract as run manifests).

Consumers reach it through ``FastembedProvider(isolation="service")``, which
falls back to the in-process pool when the service is unreachable — setups
that never run a gateway lose nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from loguru import logger

DISCOVERY_FILENAME = "embed-server.json"


# ---------------------------------------------------------------------------
# Result cache
# ---------------------------------------------------------------------------


class EmbedResultCache:
    """Sqlite-backed LRU of computed vectors.

    Keyed by (model, kind, sha256(text)) — ``kind`` (passage vs query)
    participates because E5-family models prefix the two differently, so the
    same text embeds to different vectors. ``max_rows`` bounds the table;
    eviction drops the least-recently-USED rows (reads bump recency).
    """

    def __init__(self, path: Path, *, max_rows: int = 200_000) -> None:
        self._path = Path(path)
        self._max_rows = max_rows
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS embed_cache ("
            " key TEXT PRIMARY KEY, vector TEXT NOT NULL,"
            " used_seq INTEGER NOT NULL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS embed_cache_used ON embed_cache(used_seq)"
        )
        self._db.commit()

    @staticmethod
    def _key(model: str, kind: str, text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{model}|{kind}|{digest}"

    def _next_seq(self) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(used_seq), 0) FROM embed_cache").fetchone()
        return int(row[0]) + 1

    def get(self, model: str, kind: str, text: str) -> list[float] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT vector FROM embed_cache WHERE key = ?",
                (self._key(model, kind, text),),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE embed_cache SET used_seq = ? WHERE key = ?",
                (self._next_seq(), self._key(model, kind, text)),
            )
            self._db.commit()
            return [float(x) for x in json.loads(row[0])]

    def put(self, model: str, kind: str, text: str, vector: list[float]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO embed_cache (key, vector, used_seq)"
                " VALUES (?, ?, ?)",
                (self._key(model, kind, text), json.dumps(vector), self._next_seq()),
            )
            count = self._db.execute(
                "SELECT COUNT(*) FROM embed_cache").fetchone()[0]
            if count > self._max_rows:
                self._db.execute(
                    "DELETE FROM embed_cache WHERE key IN ("
                    " SELECT key FROM embed_cache ORDER BY used_seq ASC LIMIT ?)",
                    (count - self._max_rows,),
                )
            self._db.commit()

    def rows(self) -> int:
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM embed_cache").fetchone()[0])


# ---------------------------------------------------------------------------
# Discovery file
# ---------------------------------------------------------------------------


def _discovery_path() -> Path:
    from durin.config.home import durin_home

    return durin_home() / DISCOVERY_FILENAME


def write_discovery(*, port: int, token: str, model: str) -> Path:
    """Publish this server's endpoint for other durin processes."""
    from durin.utils.atomic_write import atomic_write_text
    from durin.utils.process_tree import process_identity

    path = _discovery_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps({
        "port": port,
        "token": token,
        "model": model,
        "owner": process_identity(),
    }))
    return path


def read_discovery() -> dict[str, Any] | None:
    """The live server's endpoint, or None (absent, malformed, dead owner)."""
    from durin.utils.process_tree import process_alive

    path = _discovery_path()
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict) or not rec.get("port") or not rec.get("token"):
        return None
    if not process_alive(rec.get("owner")):
        return None
    return rec


def clear_discovery() -> None:
    try:
        _discovery_path().unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------


def build_embed_app(provider: Any, *, token: str, cache: EmbedResultCache | None):
    """Build the Starlette app around an already-constructed provider.

    ``provider`` is any object with ``model_name`` / ``embed_passages`` /
    ``embed_query`` — in production a ``FastembedProvider(isolation="inline")``
    (inline is CORRECT here: this process exists to hold the model).
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def _authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(header[7:], token)

    async def embeddings(request: Request):
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        raw_input = body.get("input")
        texts = [raw_input] if isinstance(raw_input, str) else raw_input
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            return JSONResponse(
                {"error": "input must be a string or list of strings"},
                status_code=400,
            )
        # "passage"/"query" apply the model's asymmetric prefixes server-side
        # (external OpenAI-compat consumers); "raw" embeds the text exactly as
        # sent — the durin client prefixes before calling, so raw keeps the
        # cache keyed on the true model input.
        kind = body.get("kind", "passage")
        if kind not in ("passage", "query", "raw"):
            return JSONResponse(
                {"error": 'kind must be "passage", "query" or "raw"'},
                status_code=400)

        model = provider.model_name
        vectors: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        for i, text in enumerate(texts):
            hit = cache.get(model, kind, text) if cache is not None else None
            if hit is not None:
                vectors[i] = hit
            else:
                missing.append(i)

        if missing:
            pending = [texts[i] for i in missing]
            import anyio

            if kind == "query":
                computed = [
                    await anyio.to_thread.run_sync(provider.embed_query, t)
                    for t in pending
                ]
            elif kind == "raw":
                computed = await anyio.to_thread.run_sync(
                    provider.embed, pending)
            else:
                computed = await anyio.to_thread.run_sync(
                    provider.embed_passages, pending)
            for i, vec in zip(missing, computed):
                vec = [float(x) for x in vec]
                vectors[i] = vec
                if cache is not None:
                    cache.put(model, kind, texts[i], vec)

        return JSONResponse({
            "object": "list",
            "model": model,
            "data": [
                {"object": "embedding", "index": i, "embedding": vec}
                for i, vec in enumerate(vectors)
            ],
        })

    async def health(request: Request):
        return JSONResponse({
            "status": "ok",
            "model": provider.model_name,
            "pid": os.getpid(),
            "cache_rows": cache.rows() if cache is not None else 0,
        })

    return Starlette(routes=[
        Route("/v1/embeddings", embeddings, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ])


def run_embed_server(*, port: int = 0, model: str | None = None) -> None:
    """Blocking entry point for the ``durin memory embed-server`` CLI.

    Binds loopback-only. ``port=0`` lets the OS pick; the bound port is
    published via the discovery file, which is removed on shutdown.
    """
    import secrets
    import socket

    import uvicorn

    from durin.config import load_config
    from durin.config.home import durin_home
    from durin.memory.embedding import provider_from_config

    config = load_config()
    provider = provider_from_config(config, model=model)
    # This process IS the isolation boundary — hold the model inline and
    # load it now: a standing service must be warm before the first caller.
    provider._isolation = "inline"
    provider.embed(["warmup"])

    token = secrets.token_hex(16)
    cache = EmbedResultCache(durin_home() / "embed-cache.sqlite")
    app = build_embed_app(provider, token=token, cache=cache)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    bound_port = sock.getsockname()[1]
    write_discovery(port=bound_port, token=token, model=provider.model_name)
    logger.info(
        "embed server ready (port={} model={})", bound_port, provider.model_name)
    try:
        uvicorn.Server(uvicorn.Config(
            app, log_level="warning", access_log=False,
        )).run(sockets=[sock])
    finally:
        clear_discovery()


def service_embed(texts: list[str], *, rec: dict) -> list[list[float]]:
    """POST raw (already-prefixed) texts to the discovered server.

    Raises on any transport/HTTP failure — the caller owns the fallback
    decision. The generous read timeout covers a large batch queued behind
    another consumer on a slow host; connect stays tight so a dead server
    fails fast.
    """
    import httpx

    resp = httpx.post(
        f"http://127.0.0.1:{rec['port']}/v1/embeddings",
        headers={"Authorization": f"Bearer {rec['token']}"},
        json={"input": texts, "kind": "raw"},
        timeout=httpx.Timeout(300.0, connect=2.0),
    )
    resp.raise_for_status()
    data = sorted(resp.json()["data"], key=lambda d: d["index"])
    return [[float(x) for x in d["embedding"]] for d in data]
