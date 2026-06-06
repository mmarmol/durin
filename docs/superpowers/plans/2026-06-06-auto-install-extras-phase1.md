# Auto-install Feature Extras — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a user activates a feature whose pip extra is missing, install it frictionlessly (progress + a webui confirm dialog) instead of failing with a console message — wired for `web_search` (ddgs) and `cross_encoder` (sentence-transformers) in Phase 1.

**Architecture:** A central `durin/extras.py` with a `feature → extra` registry and an `ensure_extra(feature, config)` helper that probes the module, resolves the extra's package specs from durin-agent's own installed metadata, and installs via pip-or-uv. Call-sites: the agent web-search tool (runtime retry) and two new aiohttp endpoints (`/api/extras/status`, `/api/extras/ensure`) that the webui MemorySettings confirm dialog drives, plus a restart endpoint that shells `durin gateway restart`. Gated by `config.install.auto_install_extras` (default on).

**Tech Stack:** Python 3.11+, Pydantic config, aiohttp WS/REST server (`durin/channels/websocket.py`), pytest, React/TypeScript webui.

**Reference:** spec at `docs/superpowers/specs/2026-06-06-auto-install-extras-design.md`.

**CI note:** CI installs durin without `[memory]`/`[local]`/`[cross-encoder]`. All tests here must use the `_FakeConfig`/mock approach below and **must not** import `sentence_transformers`/`ddgs` for real, or they fail CI. (See memory: CI skips heavy extras.)

---

## File Structure

- **Create** `durin/extras.py` — registry (`REGISTRY`), `FeatureExtra`, `EnsureResult`, `ensure_extra()`, installer detection, post-install hooks. One responsibility: deciding/installing a feature's extra.
- **Create** `tests/test_extras.py` — unit tests (subprocess + import mocked; no real heavy installs).
- **Modify** `durin/config/schema.py` (`InstallConfig`, ~line 787) — add `auto_install_extras: bool = True`.
- **Modify** `durin/agent/tools/web.py` (`_search_duckduckgo`, ~line 420) — on ddgs ImportError, `ensure_extra("web_search")` + retry.
- **Modify** `durin/channels/websocket.py` (`_dispatch_http`, ~line 710; new handlers) — `GET /api/extras/status`, `POST /api/extras/ensure`, `POST /api/extras/restart`.
- **Modify** `durin/memory/cross_encoder.py` — expose a `reset_global()` that clears `_RERANK_FALLBACK_LOGGED` (used by the post-install hook).
- **Modify** `webui/src/lib/api.ts` — `getExtraStatus()`, `ensureExtra()`, `restartGateway()`.
- **Modify** `webui/src/components/settings/MemorySettings.tsx` — confirm dialog before enabling/testing the reranker when the extra is missing.
- **Test** `tests/test_extras_endpoints.py` — the three aiohttp handlers (installer mocked).

---

### Task 1: Registry + dataclasses in `durin/extras.py`

**Files:**
- Create: `durin/extras.py`
- Test: `tests/test_extras.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extras.py
import tomllib
from pathlib import Path

from durin.extras import REGISTRY, FeatureExtra


def test_registry_extras_exist_in_pyproject():
    """Every registry entry maps to a real pyproject extra (catches typos)."""
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    declared = set(pyproject["project"]["optional-dependencies"])
    for fe in REGISTRY.values():
        assert isinstance(fe, FeatureExtra)
        assert fe.extra in declared, f"{fe.feature}: extra '{fe.extra}' not in pyproject"


def test_phase1_features_present():
    assert REGISTRY["web_search"].module == "ddgs"
    assert REGISTRY["web_search"].needs_restart is False
    assert REGISTRY["cross_encoder"].module == "sentence_transformers"
    assert REGISTRY["cross_encoder"].needs_restart is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/durin-improvements && python -m pytest tests/test_extras.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'durin.extras'`

- [ ] **Step 3: Write minimal implementation**

```python
# durin/extras.py
"""Auto-install of optional feature extras on activation.

A feature maps to a pip extra declared in pyproject `[project.optional-dependencies]`.
When a feature is activated but its extra is missing, `ensure_extra` installs it
(gated by `config.install.auto_install_extras`). See
docs/superpowers/specs/2026-06-06-auto-install-extras-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureExtra:
    feature: str         # stable key used by call-sites
    extra: str           # pyproject extra name -> durin-agent[<extra>]
    module: str          # importable module proving the extra is present
    needs_restart: bool  # dep only takes effect after a gateway restart
    approx_size: str     # human download estimate for the confirm dialog
    label: str           # human feature name


# Phase-1 probe modules (ddgs, sentence_transformers) are verified. Phase-2
# entries' modules are best-effort and re-confirmed when each is wired.
REGISTRY: dict[str, FeatureExtra] = {
    "web_search": FeatureExtra("web_search", "web", "ddgs", False, "~5 MB", "Web search"),
    "cross_encoder": FeatureExtra("cross_encoder", "cross-encoder", "sentence_transformers", True, "~1 GB", "Cross-encoder reranker"),
    "mcp": FeatureExtra("mcp", "mcp", "mcp", True, "~10 MB", "MCP servers"),
    "slack": FeatureExtra("slack", "slack", "slack_sdk", True, "~10 MB", "Slack channel"),
    "discord": FeatureExtra("discord", "discord", "discord", True, "~10 MB", "Discord channel"),
    "memory_vector": FeatureExtra("memory_vector", "memory", "fastembed", True, "~400 MB", "Vector memory"),
    "local_models": FeatureExtra("local_models", "local", "llama_cpp", True, "~200 MB", "Local models"),
    "oauth": FeatureExtra("oauth", "oauth", "oauth_cli_kit", False, "~5 MB", "OAuth providers"),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_extras.py -q`
Expected: PASS (2 passed). If `test_registry_extras_exist_in_pyproject` fails, fix the offending `extra=` string to match pyproject — do NOT change pyproject.

- [ ] **Step 5: Commit**

```bash
git add durin/extras.py tests/test_extras.py
git commit -m "feat(extras): feature->extra registry"
```

---

### Task 2: `ensure_extra` — probe, resolve specs, install (mechanism detection)

**Files:**
- Modify: `durin/extras.py`
- Test: `tests/test_extras.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extras.py (append)
import types
import durin.extras as ex


class _Cfg:
    def __init__(self, auto=True):
        self.install = types.SimpleNamespace(auto_install_extras=auto)


def test_present_module_is_noop(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: True)
    called = []
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: called.append(specs) or None)
    r = ex.ensure_extra("web_search", config=_Cfg())
    assert r.status == "present"
    assert called == []  # never tried to install


def test_gate_off_returns_disabled(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    r = ex.ensure_extra("web_search", config=_Cfg(auto=False))
    assert r.status == "disabled"
    assert "durin-agent[web]" in r.message


def test_install_success(monkeypatch):
    seen = {"present": False}
    monkeypatch.setattr(ex, "_module_present", lambda m: seen["present"])
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["ddgs>=1,<2"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: ["echo", *specs])

    def fake_run(cmd, **kw):
        seen["present"] = True  # install "worked"
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    r = ex.ensure_extra("web_search", config=_Cfg())
    assert r.status == "installed"
    assert r.needs_restart is False


def test_install_failure_returns_message(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["ddgs>=1,<2"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: ["false", *specs])

    def fake_run(cmd, **kw):
        raise ex.subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    r = ex.ensure_extra("web_search", config=_Cfg())
    assert r.status == "failed"
    assert "boom" in r.message


def test_no_installer_found(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    monkeypatch.setattr(ex, "_extra_specs", lambda extra: ["ddgs"])
    monkeypatch.setattr(ex, "_installer_cmd", lambda specs: None)
    r = ex.ensure_extra("web_search", config=_Cfg())
    assert r.status == "failed"
    assert "installer" in r.message.lower()


def test_installer_prefers_pip_then_uv(monkeypatch):
    monkeypatch.setattr(ex, "_module_present", lambda m: m == "pip")
    cmd = ex._installer_cmd(["pkg"])
    assert cmd[:3] == [ex.sys.executable, "-m", "pip"]
    monkeypatch.setattr(ex, "_module_present", lambda m: False)
    monkeypatch.setattr(ex.shutil, "which", lambda n: "/usr/bin/uv" if n == "uv" else None)
    cmd = ex._installer_cmd(["pkg"])
    assert cmd[0] == "/usr/bin/uv" and "pip" in cmd and "--python" in cmd
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_extras.py -q`
Expected: FAIL — `AttributeError: module 'durin.extras' has no attribute 'ensure_extra'`

- [ ] **Step 3: Write minimal implementation (append to `durin/extras.py`)**

```python
import importlib
import importlib.metadata
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field

import logging

logger = logging.getLogger(__name__)

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass
class EnsureResult:
    status: str               # "present" | "installed" | "failed" | "disabled"
    feature: str
    needs_restart: bool = False
    message: str = ""


def _module_present(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def _extra_specs(extra: str) -> list[str]:
    """Package specs for durin-agent's <extra>, from installed metadata.

    Avoids duplicating pyproject pins. A requirement line looks like
    ``sentence-transformers>=3.0,<6.0; extra == "cross-encoder"``.
    """
    specs: list[str] = []
    for req in importlib.metadata.requires("durin-agent") or []:
        if f'extra == "{extra}"' in req:
            specs.append(req.split(";", 1)[0].strip())
    return specs


def _installer_cmd(specs: list[str]) -> list[str] | None:
    if not specs:
        return None
    if _module_present("pip"):
        return [sys.executable, "-m", "pip", "install", *specs]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, *specs]
    return None


def _lock_for(feature: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(feature, threading.Lock())


def ensure_extra(feature: str, *, config) -> EnsureResult:
    """Ensure ``feature``'s pip extra is importable, installing it if allowed."""
    fe = REGISTRY[feature]
    if _module_present(fe.module):
        return EnsureResult("present", feature, fe.needs_restart)
    if not getattr(config.install, "auto_install_extras", True):
        return EnsureResult(
            "disabled", feature, fe.needs_restart,
            f"Run: pip install durin-agent[{fe.extra}]",
        )
    with _lock_for(feature):
        if _module_present(fe.module):
            return EnsureResult("present", feature, fe.needs_restart)
        specs = _extra_specs(fe.extra)
        cmd = _installer_cmd(specs)
        if not cmd:
            return EnsureResult(
                "failed", feature, fe.needs_restart,
                "No installer (pip or uv) found on PATH.",
            )
        logger.info("extras: installing {} -> {}", feature, specs)
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            return EnsureResult("failed", feature, fe.needs_restart, (e.stderr or "")[-800:])
        importlib.invalidate_caches()
        if not _module_present(fe.module):
            return EnsureResult(
                "failed", feature, fe.needs_restart,
                "Installed but the module is still not importable.",
            )
        _post_install(feature)
        return EnsureResult("installed", feature, fe.needs_restart)


def _post_install(feature: str) -> None:
    """Per-feature in-process cleanup so the dep takes effect without restart
    where possible. Filled in by Task 4."""
    return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_extras.py -q`
Expected: PASS (all). 

- [ ] **Step 5: Commit**

```bash
git add durin/extras.py tests/test_extras.py
git commit -m "feat(extras): ensure_extra with pip/uv detection + metadata-resolved specs"
```

---

### Task 3: Config gate `auto_install_extras`

**Files:**
- Modify: `durin/config/schema.py` (`InstallConfig`, ~line 787-798)
- Test: `tests/test_extras.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extras.py (append)
def test_install_config_has_auto_install_default_true():
    from durin.config.schema import InstallConfig
    assert InstallConfig().auto_install_extras is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_extras.py::test_install_config_has_auto_install_default_true -q`
Expected: FAIL — `AttributeError: 'InstallConfig' object has no attribute 'auto_install_extras'`

- [ ] **Step 3: Add the field (in `durin/config/schema.py`, inside `InstallConfig`, after `extras`)**

```python
    extras: list[str] = Field(default_factory=list)
    auto_install_extras: bool = Field(
        default=True,
        description=(
            "Auto-install a feature's pip extra when it's activated (frictionless). "
            "Off falls back to a 'pip install durin-agent[X]' message."
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_extras.py::test_install_config_has_auto_install_default_true -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/config/schema.py tests/test_extras.py
git commit -m "feat(config): install.auto_install_extras gate (default on)"
```

---

### Task 4: Cross-encoder post-install reset hook

**Files:**
- Modify: `durin/memory/cross_encoder.py` (module flag `_RERANK_FALLBACK_LOGGED`, ~line 163)
- Modify: `durin/extras.py` (`_post_install`)
- Test: `tests/test_extras.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extras.py (append)
def test_post_install_cross_encoder_clears_fallback_flag(monkeypatch):
    import durin.memory.cross_encoder as ce
    ce._RERANK_FALLBACK_LOGGED = True
    ex._post_install("cross_encoder")
    assert ce._RERANK_FALLBACK_LOGGED is False


def test_post_install_unknown_is_noop():
    ex._post_install("web_search")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_extras.py -k post_install -q`
Expected: FAIL — flag stays `True` (the hook is a no-op stub).

- [ ] **Step 3: Add `reset_global` to `cross_encoder.py` and call it from `_post_install`**

In `durin/memory/cross_encoder.py`, add near the module flag:

```python
def reset_global() -> None:
    """Clear the process-wide 'reranker is off' latch so the next score retries
    the (possibly just-installed) model. Mirrors health_check's reset."""
    global _RERANK_FALLBACK_LOGGED
    _RERANK_FALLBACK_LOGGED = False
```

In `durin/extras.py`, replace `_post_install`:

```python
def _post_install(feature: str) -> None:
    """Per-feature in-process cleanup so the dep takes effect without restart
    where possible."""
    if feature == "cross_encoder":
        try:
            from durin.memory import cross_encoder
            cross_encoder.reset_global()
        except Exception:  # pragma: no cover - best effort
            logger.debug("extras: cross_encoder reset_global failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_extras.py -k post_install -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/memory/cross_encoder.py durin/extras.py tests/test_extras.py
git commit -m "feat(extras): post-install cross-encoder reset (avoid restart)"
```

---

### Task 5: Wire `web_search` runtime fallback

**Files:**
- Modify: `durin/agent/tools/web.py` (`_search_duckduckgo`, ~line 420-440)
- Test: `tests/agent/tools/test_web_extras.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/tools/test_web_extras.py
import types
import pytest
import durin.agent.tools.web as web


@pytest.mark.asyncio
async def test_ddgs_missing_triggers_ensure_then_retry(monkeypatch):
    calls = {"ensure": 0, "search": 0}

    def fake_ensure(feature, *, config):
        calls["ensure"] += 1
        return types.SimpleNamespace(status="installed", needs_restart=False, message="")

    monkeypatch.setattr(web, "ensure_extra", fake_ensure, raising=False)

    # First import attempt fails, post-install attempt "succeeds".
    seq = iter([ImportError("No module named 'ddgs'"), None])

    def fake_import_ddgs():
        err = next(seq)
        if err:
            raise err
        calls["search"] += 1
        return ["result"]

    monkeypatch.setattr(web, "_ddgs_text", fake_import_ddgs, raising=False)
    tool = web.WebSearchTool.__new__(web.WebSearchTool)
    tool.config = types.SimpleNamespace(timeout=5)
    tool._app_config = types.SimpleNamespace(install=types.SimpleNamespace(auto_install_extras=True))
    out = await tool._search_duckduckgo("q", 1)
    assert calls["ensure"] == 1
    assert calls["search"] == 1
    assert "result" in out
```

(Adjust the monkeypatched seam names to match the refactor in Step 3.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/agent/tools/test_web_extras.py -q`
Expected: FAIL — current `_search_duckduckgo` has no `ensure_extra` seam.

- [ ] **Step 3: Refactor `_search_duckduckgo` to ensure-then-retry**

Extract the ddgs call into a helper and wrap the import error:

```python
# durin/agent/tools/web.py (near the top of the module)
from durin.extras import ensure_extra


def _ddgs_text(query: str, n: int):
    from ddgs import DDGS
    return DDGS(timeout=10).text(query, max_results=n)


# inside WebSearchTool._search_duckduckgo, replacing the try body:
    async def _search_duckduckgo(self, query: str, n: int) -> str:
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(_ddgs_text, query, n),
                timeout=self.config.timeout,
            )
        except ImportError:
            res = ensure_extra("web_search", config=self._app_config)
            if res.status not in ("present", "installed"):
                return f"Error: web search unavailable — {res.message}"
            try:
                raw = await asyncio.wait_for(
                    asyncio.to_thread(_ddgs_text, query, n),
                    timeout=self.config.timeout,
                )
            except Exception as e:  # noqa: BLE001
                return f"Error: DuckDuckGo search failed ({e})"
        except Exception as e:  # noqa: BLE001
            logger.warning("DuckDuckGo search failed: {}", e)
            return f"Error: DuckDuckGo search failed ({e})"
        # ... existing result-formatting on `raw` ...
```

(If `WebSearchTool` does not already hold `self._app_config`, thread it from `create(cls, ctx)` via `getattr(ctx, "app_config", None)` — follow the same pattern other tools use.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/agent/tools/test_web_extras.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/agent/tools/web.py tests/agent/tools/test_web_extras.py
git commit -m "feat(extras): web_search auto-installs ddgs on first use"
```

---

### Task 6: Webui REST endpoints (`status`, `ensure`, `restart`)

**Files:**
- Modify: `durin/channels/websocket.py` (`_dispatch_http` router ~line 710; new `_handle_extras_*`)
- Test: `tests/test_extras_endpoints.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extras_endpoints.py
import json
import types
import durin.channels.websocket as ws


def _server():
    s = ws.WebSocketServer.__new__(ws.WebSocketServer)
    s._check_api_token = lambda req: True
    s._config = types.SimpleNamespace(install=types.SimpleNamespace(auto_install_extras=True))
    return s


def test_status_reports_present(monkeypatch):
    import durin.extras as ex
    monkeypatch.setattr(ex, "_module_present", lambda m: True)
    s = _server()
    resp = s._handle_extras_status({"feature": "web_search"})
    body = json.loads(resp.body)
    assert body["present"] is True
    assert body["extra"] == "web"
    assert body["needs_restart"] is False


def test_ensure_invokes_ensure_extra(monkeypatch):
    import durin.extras as ex
    monkeypatch.setattr(
        ex, "ensure_extra",
        lambda feature, *, config: ex.EnsureResult("installed", feature, True, ""),
    )
    s = _server()
    resp = s._handle_extras_ensure({"feature": "cross_encoder", "restart": False})
    body = json.loads(resp.body)
    assert body["status"] == "installed"
    assert body["needs_restart"] is True


def test_unknown_feature_400():
    s = _server()
    resp = s._handle_extras_status({"feature": "nope"})
    assert resp.status == 400
```

(Match `_http_json_response`/`_http_error` return shapes used elsewhere in `websocket.py`; the assertions above assume a `.body` JSON string + `.status` int — adapt to the actual `Response` type, e.g. `aiohttp.web.Response`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_extras_endpoints.py -q`
Expected: FAIL — `_handle_extras_status` does not exist.

- [ ] **Step 3: Add the handlers + routes in `durin/channels/websocket.py`**

Add to `_dispatch_http` (next to the other `/api/...` checks):

```python
        if got == "/api/extras/status":
            return self._handle_extras_status(query)
        if got == "/api/extras/ensure":
            return self._handle_extras_ensure(self._json_body(request))
        if got == "/api/extras/restart":
            return self._handle_extras_restart()
```

Add the handlers (mirror existing `_handle_*` style: token check, `_http_json_response`, `_http_error`):

```python
    def _handle_extras_status(self, query: dict) -> Any:
        if not self._check_api_token_query(query):   # or the existing token check
            return _http_error(401, "Unauthorized")
        from durin.extras import REGISTRY, _module_present
        feature = (query or {}).get("feature", "")
        fe = REGISTRY.get(feature)
        if fe is None:
            return _http_error(400, f"unknown feature '{feature}'")
        return _http_json_response({
            "present": _module_present(fe.module),
            "extra": fe.extra,
            "approx_size": fe.approx_size,
            "needs_restart": fe.needs_restart,
            "label": fe.label,
        })

    def _handle_extras_ensure(self, body: dict) -> Any:
        from durin.extras import REGISTRY, ensure_extra
        feature = (body or {}).get("feature", "")
        if feature not in REGISTRY:
            return _http_error(400, f"unknown feature '{feature}'")
        res = ensure_extra(feature, config=self._config)
        out = {
            "status": res.status, "needs_restart": res.needs_restart,
            "message": res.message,
        }
        if res.status == "installed" and body.get("restart") and res.needs_restart:
            self._spawn_gateway_restart()
            out["restarting"] = True
        return _http_json_response(out)

    def _handle_extras_restart(self) -> Any:
        self._spawn_gateway_restart()
        return _http_json_response({"restarting": True})

    def _spawn_gateway_restart(self) -> None:
        import subprocess, sys
        # Detached so it survives this process being killed by the restart.
        subprocess.Popen(
            [sys.executable, "-m", "durin", "gateway", "restart"],
            start_new_session=True,
        )
```

(Use the actual token-check helper the other handlers use; if POST bodies are parsed elsewhere, reuse that — `self._json_body(request)` is a placeholder for the existing body parser.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_extras_endpoints.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/channels/websocket.py tests/test_extras_endpoints.py
git commit -m "feat(extras): /api/extras status, ensure, restart endpoints"
```

---

### Task 7: Webui API client functions

**Files:**
- Modify: `webui/src/lib/api.ts` (near `testCrossEncoderModel`, ~line 602)

- [ ] **Step 1: Add the client functions**

```typescript
// webui/src/lib/api.ts
export interface ExtraStatus {
  present: boolean; extra: string; approx_size: string;
  needs_restart: boolean; label: string;
}

export async function getExtraStatus(token: string, feature: string): Promise<ExtraStatus> {
  const q = new URLSearchParams({ feature });
  const r = await fetch(`${base}/api/extras/status?${q.toString()}`, authHeaders(token));
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export interface EnsureResult {
  status: "present" | "installed" | "failed" | "disabled";
  needs_restart: boolean; message: string; restarting?: boolean;
}

export async function ensureExtra(
  token: string, feature: string, restart: boolean,
): Promise<EnsureResult> {
  const r = await fetch(`${base}/api/extras/ensure`, {
    method: "POST",
    headers: { ...authHeaders(token).headers, "Content-Type": "application/json" },
    body: JSON.stringify({ feature, restart }),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
```

(Use the file's existing `base` const and auth-header helper — match how `testCrossEncoderModel` builds its request.)

- [ ] **Step 2: Verify it compiles**

Run: `cd webui && npx tsc -p tsconfig.build.json --noEmit`
Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add webui/src/lib/api.ts
git commit -m "feat(webui): extras status/ensure API client"
```

---

### Task 8: MemorySettings confirm dialog for the reranker

**Files:**
- Modify: `webui/src/components/settings/MemorySettings.tsx` (toggle ~line 176-190; "Probar" `runTest` ~line 476-492)

- [ ] **Step 1: Gate enabling + testing behind an extras check**

Before enabling the toggle or running the test, check status; if the extra is missing, open a confirm dialog showing `extra` + `approx_size` + (if `needs_restart`) a restart checkbox; on confirm call `ensureExtra(token, "cross_encoder", restart)`, show progress, then proceed (enable / run test). Use inline styled confirmation (no `window.confirm`).

```typescript
// state
const [installPrompt, setInstallPrompt] = useState<null | {
  status: ExtraStatus; restart: boolean; busy: boolean; error?: string;
  after: () => void;
}>(null);

async function withExtra(feature: string, after: () => void) {
  const st = await getExtraStatus(token, feature);
  if (st.present) { after(); return; }
  setInstallPrompt({ status: st, restart: st.needs_restart, busy: false, after });
}

async function confirmInstall() {
  if (!installPrompt) return;
  setInstallPrompt({ ...installPrompt, busy: true, error: undefined });
  const res = await ensureExtra(token, installPrompt.status.label && "cross_encoder", installPrompt.restart);
  if (res.status === "failed" || res.status === "disabled") {
    setInstallPrompt({ ...installPrompt, busy: false, error: res.message });
    return;
  }
  const after = installPrompt.after;
  setInstallPrompt(null);
  if (!(res.restarting)) after();  // if restarting, the gateway will reload
}
```

Wire the toggle's `onClick` to `void withExtra("cross_encoder", () => onSave("memory.search.cross_encoder.enabled", !crossEncoder.enabled))` and `runTest` to run inside `withExtra("cross_encoder", actualRunTest)`.

- [ ] **Step 2: Render the confirm dialog**

```tsx
{installPrompt && (
  <div className="install-confirm">
    <p>{t("settings.extras.willInstall", {
      extra: installPrompt.status.extra, size: installPrompt.status.approx_size,
    })}</p>
    {installPrompt.status.needs_restart && (
      <label>
        <input type="checkbox" checked={installPrompt.restart}
          onChange={(e) => setInstallPrompt({ ...installPrompt, restart: e.target.checked })} />
        {t("settings.extras.restartAfter")}
      </label>
    )}
    {installPrompt.error && <p className="error">{installPrompt.error}</p>}
    <button disabled={installPrompt.busy} onClick={() => void confirmInstall()}>
      {installPrompt.busy ? t("settings.extras.installing") : t("settings.extras.install")}
    </button>
    <button disabled={installPrompt.busy} onClick={() => setInstallPrompt(null)}>
      {t("common.cancel")}
    </button>
  </div>
)}
```

- [ ] **Step 3: Add the i18n keys**

Add to `webui/src/i18n/locales/{es,en,id}/common.json` under `settings.extras`:
- `willInstall`: es "Esto instalará {{extra}} (~{{size}} de descarga)." / en "This will install {{extra}} (~{{size}} download)." / id translate.
- `restartAfter`, `install`, `installing` — translate per locale.

- [ ] **Step 4: Verify build**

Run: `cd webui && npx tsc -p tsconfig.build.json --noEmit && for f in es en id; do python3 -m json.tool src/i18n/locales/$f/common.json >/dev/null; done`
Expected: no TS errors; all JSON valid.

- [ ] **Step 5: Commit**

```bash
git add webui/src/components/settings/MemorySettings.tsx webui/src/i18n/locales
git commit -m "feat(webui): reranker install confirm dialog (size + restart)"
```

---

### Task 9: Full backend gate + manual verification

- [ ] **Step 1: Run the Python suite for touched areas**

Run: `python -m pytest tests/test_extras.py tests/test_extras_endpoints.py tests/agent/tools/test_web_extras.py -q`
Expected: all PASS.

- [ ] **Step 2: Confirm no real heavy imports leak into tests**

Run: `python -m pytest tests/test_extras.py -q -W error::ImportWarning`
Expected: PASS without importing `sentence_transformers`/`ddgs` for real.

- [ ] **Step 3: Manual webui smoke (after a worktree build, when deploy is allowed)**

Build webui, run `python -m durin gateway --foreground` from the worktree, open the dashboard, toggle the reranker with the extra absent → confirm dialog shows `cross-encoder (~1 GB)` + restart checkbox → install → success. (Do NOT redeploy the user's daily driver per their instruction.)

- [ ] **Step 4: Commit any fixups**

```bash
git add -A && git commit -m "test(extras): phase-1 gate green"
```

---

## Self-Review

**Spec coverage:** registry (T1) · ensure_extra + mechanism detection (T2) · gate (T3) · cross-encoder cache-reset optimization (T4) · agent runtime surface for web_search (T5) · webui endpoints + restart (T6) · webui client + confirm dialog with size + restart checkbox (T7-T8) · error handling (failed/disabled/no-installer in T2, surfaced in T5/T8) · testing (T1-T6, T9). Agent surface for cross_encoder is via the webui "Probar"/toggle (T6/T8) plus the existing `probe_model`; the dedicated `ask_user_question` agent path for cross_encoder is **Phase 2** (the runtime reranker fallback) — noted, not in Phase 1 scope. Onboarding + the other extras: Phase 2.

**Placeholder scan:** no TBDs. Two seams are explicitly marked "match the existing helper" (token check, POST-body parser, `base`/auth in api.ts) because they must follow `websocket.py`/`api.ts` conventions discovered at implementation time — each names the concrete existing function to mirror.

**Type consistency:** `EnsureResult{status,feature,needs_restart,message}` and `FeatureExtra{feature,extra,module,needs_restart,approx_size,label}` are used identically across T1/T2/T6; the webui `ExtraStatus`/`EnsureResult` shapes match the endpoint JSON in T6.
