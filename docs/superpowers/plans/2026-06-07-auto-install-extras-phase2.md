# Auto-install Feature Extras — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing `ensure_extra` helper (Phase 1) at the remaining feature activation points — channels (slack/discord), mcp, memory (fastembed), local models (llama_cpp), oauth (oauth_cli_kit) — plus make the onboarding wizard install the extras of every feature the user selects.

**Architecture:** Reuse Phase 1's `durin.extras.ensure_extra(feature, *, config)` + `REGISTRY` (probe modules already verified correct). Two surfaces: (1) **runtime** — at each `ImportError`/SDK-missing site, call `ensure_extra`; if the dep loads without restart, retry in-process (mcp), else install + log "restart to activate" (slack/discord/local/memory — their module-level import/flag was already evaluated). (2) **onboarding** — the wizard already runs `pip install durin-agent[<extras_to_install>]`; Phase 2 adds the remaining features to that set. A shared `ensure_or_note(feature, config)` helper centralises the runtime "install, then retry-or-note-restart" decision.

**Tech Stack:** Python 3.11+, the Phase-1 `durin/extras.py`, pytest. No webui changes required (channels/mcp/local/oauth have no per-feature webui toggle today; they activate via config + onboarding + runtime).

**Reference:** spec `docs/superpowers/specs/2026-06-06-auto-install-extras-design.md`; Phase-1 plan `docs/superpowers/plans/2026-06-06-auto-install-extras-phase1.md`.

**Merge note:** origin/main merged Codex OAuth (`durin/utils/oauth.py`, `durin/providers/openai_codex_provider.py`, `durin/providers/codex_device_auth.py`, codex routes in `durin/channels/websocket.py`, `durin/cli/onboard_wizard.py`). Task 6 (oauth) integrates with that merged code — read it before editing.

**CI note:** none of these extras (slack/discord/mcp/memory/local/oauth) are installed in CI. All tests must mock `ensure_extra`/imports — never import the real SDKs.

---

## File Structure

- **Modify** `durin/extras.py` — add `ensure_or_note(feature, config) -> EnsureResult` (runtime convenience: calls `ensure_extra`, logs the outcome; callers branch on `.status`/`.needs_restart`).
- **Modify** `durin/channels/slack.py` (`SlackChannel.start`, ~line 88) — ensure `slack` before using slack_sdk.
- **Modify** `durin/channels/discord.py` (`DiscordChannel.start`, ~line 384) — ensure `discord`; needs restart (module-level `DISCORD_AVAILABLE`).
- **Modify** `durin/agent/loop.py` (`_connect_mcp`, ~line 708) — ensure `mcp` on ImportError, retry once (lazy import → in-process retry works).
- **Modify** `durin/memory/embedding.py` (`list_supported_models`, ~line 159) — ensure `memory_vector` before raising.
- **Modify** `durin/providers/local_llama_provider.py` (`_load_llama`, ~line 173) — ensure `local_models` before raising ImportError.
- **Modify** `durin/cli/onboard_wizard.py` (`_reconcile_extras_from_config`, ~line 265) — add slack/discord/local/oauth extras from final config.
- **Tests:** `tests/test_extras.py` (ensure_or_note), `tests/channels/test_slack_extras.py`, `tests/channels/test_discord_extras.py`, `tests/agent/test_loop_mcp_extras.py`, `tests/memory/test_embedding_extras.py`, `tests/providers/test_local_llama_extras.py`, `tests/cli/test_onboard_extras.py`.

---

### Task 1: `ensure_or_note` runtime helper

**Files:**
- Modify: `durin/extras.py`
- Test: `tests/test_extras.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_extras.py (append)
def test_ensure_or_note_logs_and_returns(monkeypatch, caplog):
    monkeypatch.setattr(
        ex, "ensure_extra",
        lambda feature, *, config: ex.EnsureResult("installed", feature, True, ""),
    )
    import logging
    with caplog.at_level(logging.INFO):
        r = ex.ensure_or_note("slack", config=None)
    assert r.status == "installed"
    assert r.needs_restart is True
    assert any("slack" in rec.message for rec in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /tmp/durin-improvements && python -m pytest tests/test_extras.py::test_ensure_or_note_logs_and_returns -q`
Expected: FAIL — `AttributeError: module 'durin.extras' has no attribute 'ensure_or_note'`

- [ ] **Step 3: Implement (append to `durin/extras.py`)**

```python
def ensure_or_note(feature: str, *, config) -> EnsureResult:
    """Runtime convenience around ``ensure_extra``: install if missing and log a
    one-line outcome. Callers inspect ``.status`` (retry if "present"/"installed"
    and not needs_restart) and ``.needs_restart`` (tell the user to restart)."""
    res = ensure_extra(feature, config=config)
    if res.status == "installed" and res.needs_restart:
        logger.info(
            "extras: installed %s — restart the gateway to activate it", feature
        )
    elif res.status == "failed":
        logger.warning("extras: could not install %s: %s", feature, res.message)
    return res
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_extras.py::test_ensure_or_note_logs_and_returns -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/extras.py tests/test_extras.py
git commit -m "feat(extras): ensure_or_note runtime helper"
```

---

### Task 2: Slack channel auto-install

**Files:**
- Modify: `durin/channels/slack.py` (`start`, ~line 88; imports at top)
- Test: `tests/channels/test_slack_extras.py`

**Context:** slack.py imports `slack_sdk.*` at module top (line 10-14), so a missing extra fails at import of the module itself. To make `start()` able to recover, move the slack_sdk imports to be lazy inside `start()` (or guard the module like discord does). Then `start()` ensures the extra and notes a restart.

- [ ] **Step 1: Write the failing test**

```python
# tests/channels/test_slack_extras.py
import types
import durin.channels.slack as slack


def test_start_missing_sdk_installs_and_notes_restart(monkeypatch):
    import asyncio
    calls = {"ensure": 0}

    def fake_ensure(feature, *, config):
        calls["ensure"] += 1
        assert feature == "slack"
        return types.SimpleNamespace(status="installed", needs_restart=True, message="")

    monkeypatch.setattr(slack, "ensure_or_note", fake_ensure, raising=False)
    monkeypatch.setattr(slack, "_import_slack_sdk", lambda: (_ for _ in ()).throw(ImportError("no slack_sdk")), raising=False)

    ch = slack.SlackChannel.__new__(slack.SlackChannel)
    ch.config = types.SimpleNamespace(bot_token="x", app_token="y")
    ch.logger = types.SimpleNamespace(error=lambda *a, **k: None, info=lambda *a, **k: None)
    ch._app_config = None
    asyncio.run(ch.start())
    assert calls["ensure"] == 1  # tried to install; channel did not crash
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/channels/test_slack_extras.py -q`
Expected: FAIL (no `_import_slack_sdk` / `ensure_or_note` seam).

- [ ] **Step 3: Implement**

In `durin/channels/slack.py`: remove the top-level `from slack_sdk... import ...` lines and add a lazy importer + the ensure call. Add near the top:

```python
from durin.extras import ensure_or_note


def _import_slack_sdk():
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.socket_mode.websockets import SocketModeClient
    from slack_sdk.web.async_client import AsyncWebClient
    return SocketModeRequest, SocketModeResponse, SocketModeClient, AsyncWebClient
```

At the start of `SlackChannel.start()` (after the token check):

```python
        try:
            (
                SocketModeRequest,
                SocketModeResponse,
                SocketModeClient,
                AsyncWebClient,
            ) = _import_slack_sdk()
        except ImportError:
            res = ensure_or_note("slack", config=getattr(self, "_app_config", None))
            if res.status == "installed":
                self.logger.error(
                    "Slack extra installed — restart the gateway to start the channel"
                )
            else:
                self.logger.error("Slack channel unavailable: %s", res.message)
            return
```

Replace the later uses of `SocketModeClient`/`AsyncWebClient` (currently relying on the module-level imports) with these locals (they're in scope within `start()`). If the classes are referenced in other methods, store them on `self` (e.g. `self._SocketModeClient = SocketModeClient`) in `start()` before use. Thread `self._app_config` from the channel's constructor (follow how other channels receive app config; if none, leave `getattr(..., None)` — `ensure_extra` tolerates None).

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/channels/test_slack_extras.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/channels/slack.py tests/channels/test_slack_extras.py
git commit -m "feat(extras): slack channel auto-installs slack_sdk (+restart note)"
```

---

### Task 3: Discord channel auto-install

**Files:**
- Modify: `durin/channels/discord.py` (`start`, ~line 384)
- Test: `tests/channels/test_discord_extras.py`

**Context:** discord.py already guards with `DISCORD_AVAILABLE = importlib.util.find_spec("discord") is not None` and logs "not installed" in `start()`. Replace that log with an `ensure_or_note` call. Because `DISCORD_AVAILABLE` and the `import discord` were evaluated at module load, a successful install still needs a restart — note it, don't retry in-process.

- [ ] **Step 1: Write the failing test**

```python
# tests/channels/test_discord_extras.py
import types
import durin.channels.discord as dc


def test_start_missing_installs_and_notes(monkeypatch):
    import asyncio
    monkeypatch.setattr(dc, "DISCORD_AVAILABLE", False)
    calls = {"ensure": 0}

    def fake_ensure(feature, *, config):
        calls["ensure"] += 1
        assert feature == "discord"
        return types.SimpleNamespace(status="installed", needs_restart=True, message="")

    monkeypatch.setattr(dc, "ensure_or_note", fake_ensure, raising=False)
    ch = dc.DiscordChannel.__new__(dc.DiscordChannel)
    ch.logger = types.SimpleNamespace(error=lambda *a, **k: None, info=lambda *a, **k: None)
    ch._app_config = None
    asyncio.run(ch.start())
    assert calls["ensure"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/channels/test_discord_extras.py -q`
Expected: FAIL (no `ensure_or_note` in discord.py).

- [ ] **Step 3: Implement**

In `durin/channels/discord.py`, add `from durin.extras import ensure_or_note` near the top imports, and replace the missing-dep branch in `start()`:

```python
        if not DISCORD_AVAILABLE:
            res = ensure_or_note("discord", config=getattr(self, "_app_config", None))
            if res.status == "installed":
                self.logger.error(
                    "Discord extra installed — restart the gateway to start the channel"
                )
            else:
                self.logger.error("Discord channel unavailable: %s", res.message)
            return
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/channels/test_discord_extras.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/channels/discord.py tests/channels/test_discord_extras.py
git commit -m "feat(extras): discord channel auto-installs discord.py (+restart note)"
```

---

### Task 4: MCP auto-install (in-process retry)

**Files:**
- Modify: `durin/agent/loop.py` (`_connect_mcp`, ~line 708)
- Test: `tests/agent/test_loop_mcp_extras.py`

**Context:** `_connect_mcp` lazily imports `connect_mcp_servers` (which imports `mcp`). Because the import is lazy (not at module load), once `mcp` is installed the retry imports it fresh — so MCP can retry in-process (no restart). `REGISTRY["mcp"].needs_restart` is `True` defensively; for the in-process retry we just attempt the import again and only note a restart if the retry still fails.

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_loop_mcp_extras.py
import types
import durin.agent.loop as loop_mod


def test_connect_mcp_installs_then_retries(monkeypatch):
    import asyncio
    calls = {"ensure": 0, "connect": 0}
    seq = iter([ImportError("no mcp"), []])

    async def fake_connect(servers, tools):
        v = next(seq)
        if isinstance(v, ImportError):
            raise v
        calls["connect"] += 1
        return v

    monkeypatch.setattr(
        "durin.agent.tools.mcp.connect_mcp_servers", fake_connect, raising=False
    )

    def fake_ensure(feature, *, config):
        calls["ensure"] += 1
        return types.SimpleNamespace(status="installed", needs_restart=False, message="")

    monkeypatch.setattr(loop_mod, "ensure_or_note", fake_ensure, raising=False)

    lp = loop_mod.AgentLoop.__new__(loop_mod.AgentLoop)
    lp._mcp_connected = False
    lp._mcp_connecting = False
    lp._mcp_servers = [object()]
    lp._mcp_stacks = None
    lp.tools = []
    lp.app_config = None
    asyncio.run(lp._connect_mcp())
    assert calls["ensure"] == 1 and calls["connect"] == 1
```

(Adjust attribute names to the real `AgentLoop._connect_mcp` body.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/agent/test_loop_mcp_extras.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add `from durin.extras import ensure_or_note` to `durin/agent/loop.py` imports. Wrap the `connect_mcp_servers` call in `_connect_mcp`:

```python
        from durin.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stacks = await connect_mcp_servers(self._mcp_servers, self.tools)
        except ImportError:
            res = ensure_or_note("mcp", config=getattr(self, "app_config", None))
            if res.status in ("present", "installed"):
                self._mcp_stacks = await connect_mcp_servers(
                    self._mcp_servers, self.tools
                )
            else:
                logger.warning("MCP unavailable: %s", res.message)
                return
```

(Preserve the existing `self._mcp_connecting`/`self._mcp_connected` bookkeeping around it.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/agent/test_loop_mcp_extras.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/agent/loop.py tests/agent/test_loop_mcp_extras.py
git commit -m "feat(extras): mcp auto-installs + retries in-process"
```

---

### Task 5: Memory vector (fastembed) auto-install

**Files:**
- Modify: `durin/memory/embedding.py` (`list_supported_models`, ~line 159)
- Test: `tests/memory/test_embedding_extras.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_embedding_extras.py
import types
import builtins
import pytest
import durin.memory.embedding as emb


def test_list_models_installs_fastembed_then_retries(monkeypatch):
    calls = {"ensure": 0}
    real_import = builtins.__import__
    state = {"present": False}

    def guarded_import(name, *a, **k):
        if name == "fastembed" and not state["present"]:
            raise ImportError("no fastembed")
        return real_import(name, *a, **k)

    def fake_ensure(feature, *, config):
        calls["ensure"] += 1
        state["present"] = True  # now importable
        return types.SimpleNamespace(status="installed", needs_restart=False, message="")

    monkeypatch.setattr(emb, "ensure_or_note", fake_ensure, raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    # With fastembed "installed" by ensure, the retry import should not raise.
    # (The real TextEmbedding import after retry will still work in CI only if
    #  installed; so assert ensure was called and no RuntimeError surfaced early.)
    with pytest.raises(Exception):
        # real fastembed isn't actually present in CI; we only assert ensure ran.
        emb.list_supported_models()
    assert calls["ensure"] == 1
```

(Note: in CI fastembed is absent, so the post-ensure import still fails — the test asserts `ensure_or_note` was invoked, which is the wiring under test. Keep the assertion on `calls["ensure"]`.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/memory/test_embedding_extras.py -q`
Expected: FAIL (no ensure call yet).

- [ ] **Step 3: Implement**

In `durin/memory/embedding.py`, add `from durin.extras import ensure_or_note` and change the `list_supported_models` guard:

```python
    try:
        from fastembed import TextEmbedding
    except ImportError:
        res = ensure_or_note("memory_vector", config=None)
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is required for vector retrieval. "
                + (res.message or "Install: pip install durin-agent[memory]")
            ) from exc
```

(Keep the rest of the function unchanged; `TextEmbedding` is used below.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/memory/test_embedding_extras.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/memory/embedding.py tests/memory/test_embedding_extras.py
git commit -m "feat(extras): memory vector auto-installs fastembed"
```

---

### Task 6: Local models (llama_cpp) auto-install

**Files:**
- Modify: `durin/providers/local_llama_provider.py` (`_load_llama`, ~line 173)
- Test: `tests/providers/test_local_llama_extras.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/test_local_llama_extras.py
import types
import durin.providers.local_llama_provider as llp


def test_load_llama_installs_then_notes(monkeypatch):
    calls = {"ensure": 0}

    def fake_ensure(feature, *, config):
        calls["ensure"] += 1
        assert feature == "local_models"
        return types.SimpleNamespace(status="installed", needs_restart=True, message="")

    monkeypatch.setattr(llp, "ensure_or_note", fake_ensure, raising=False)
    prov = llp.LocalLLMProvider.__new__(llp.LocalLLMProvider)
    prov._app_config = None
    import pytest
    with pytest.raises(Exception):
        prov._load_llama("/tmp/model.gguf", "m")  # llama_cpp absent in CI
    assert calls["ensure"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/providers/test_local_llama_extras.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `durin/providers/local_llama_provider.py`, add `from durin.extras import ensure_or_note`, and in `_load_llama`:

```python
        try:
            from llama_cpp import Llama
        except ImportError:
            res = ensure_or_note("local_models", config=getattr(self, "_app_config", None))
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise ImportError(
                    "llama-cpp-python is required for local models. "
                    + (res.message or "Install: pip install durin-agent[local]")
                ) from exc
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/providers/test_local_llama_extras.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/providers/local_llama_provider.py tests/providers/test_local_llama_extras.py
git commit -m "feat(extras): local models auto-install llama_cpp"
```

---

### Task 7: Onboarding installs every selected feature's extra

**Files:**
- Modify: `durin/cli/onboard_wizard.py` (`_reconcile_extras_from_config`, ~line 265-279)
- Test: `tests/cli/test_onboard_extras.py`

**Context:** The wizard already collects `extras` and the caller runs `pip install durin-agent[<extras>]`. `_reconcile_extras_from_config` already adds `"memory"` when `config.memory.enabled`. Extend it to also add `slack`/`discord` (when the channel is enabled), `local` (when a local provider is selected), and `oauth` (when an OAuth provider — e.g. `openai_codex` — is selected). This is where "selecting a feature in onboarding installs its dep" lands (the user's requirement).

- [ ] **Step 1: Write the failing test**

```python
# tests/cli/test_onboard_extras.py
import types
import durin.cli.onboard_wizard as ow


def _cfg(**kw):
    # minimal stand-in mirroring the attributes _reconcile reads
    return types.SimpleNamespace(**kw)


def test_reconcile_adds_channel_and_oauth_extras():
    extras = set()
    config = _cfg(
        memory=types.SimpleNamespace(enabled=False),
        channels=types.SimpleNamespace(
            slack=types.SimpleNamespace(enabled=True),
            discord=types.SimpleNamespace(enabled=True),
        ),
        agents=types.SimpleNamespace(
            defaults=types.SimpleNamespace(provider="openai_codex")
        ),
    )
    ow._reconcile_extras_from_config(config, extras)
    assert {"slack", "discord", "oauth"}.issubset(extras)
```

(Match the real signature/shape of `_reconcile_extras_from_config` + how it reads config — adapt the stand-in to the actual attributes; if it takes the `extras` set differently, follow that.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/cli/test_onboard_extras.py -q`
Expected: FAIL (slack/discord/oauth not added).

- [ ] **Step 3: Implement**

In `_reconcile_extras_from_config`, after the existing `memory` check, add (using `getattr` guards so partial configs don't crash):

```python
    channels = getattr(config, "channels", None)
    if getattr(getattr(channels, "slack", None), "enabled", False):
        extras.add("slack")
    if getattr(getattr(channels, "discord", None), "enabled", False):
        extras.add("discord")
    provider = getattr(
        getattr(getattr(config, "agents", None), "defaults", None), "provider", ""
    )
    if provider in ("openai_codex", "github_copilot"):
        extras.add("oauth")
    if provider in ("local", "local_llama"):
        extras.add("local")
```

(Use the EXACT provider id strings the wizard writes — confirm against `PROVIDER_CHOICES` in onboard_wizard.py; `openai_codex` is confirmed from the merge.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/cli/test_onboard_extras.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/cli/onboard_wizard.py tests/cli/test_onboard_extras.py
git commit -m "feat(extras): onboarding installs channel/oauth/local extras on selection"
```

---

### Task 8: Full gate

- [ ] **Step 1: Run the touched-area suites**

Run: `python -m pytest tests/test_extras.py tests/channels/ tests/agent/test_loop_mcp_extras.py tests/memory/test_embedding_extras.py tests/providers/test_local_llama_extras.py tests/cli/test_onboard_extras.py -q`
Expected: all PASS.

- [ ] **Step 2: Full regression**

Run: `python -m pytest tests/ -q`
Expected: all PASS (no real SDK imports leaked).

- [ ] **Step 3: Commit any fixups**

```bash
git add -A && git commit -m "test(extras): phase-2 gate green"
```

---

## Self-Review

**Spec coverage:** Phase-2 list — slack (T2), discord (T3), mcp (T4), memory_vector (T5), local_models (T6), oauth (T7 onboarding; runtime oauth left to the merged Codex paths' own graceful degradation — see note), onboarding wizard (T7). The shared runtime helper (T1) covers the "install then retry-or-note-restart" decision the spec's restart-handling describes.

**oauth runtime scope:** the merged Codex OAuth already degrades gracefully (returns no token, the webui shows a connect card) rather than crashing, so the *runtime* oauth auto-install is lower value than the onboarding path (T7), which installs `oauth` exactly when the user picks an OAuth provider — the intentional-activation moment. A follow-up can add `ensure_extra("oauth")` inside the codex connect handler if desired; flagged, not built (YAGNI given graceful degradation already exists).

**Placeholder scan:** no TBDs. Each task names the concrete file/line + the real seam; tests note where to match the actual signatures (slack/discord/loop/onboard internals) discovered at implementation time.

**Type consistency:** all sites call `ensure_or_note(feature, *, config) -> EnsureResult` (T1) and branch on `.status` ∈ {present, installed, failed, disabled} and `.needs_restart` — identical to the Phase-1 `EnsureResult` shape.

**Restart semantics:** mcp (T4) retries in-process (lazy import). slack/discord/memory/local install but require a restart to actually load (module-level import/flag already evaluated) → they log a restart note; the dep is in place for the next start.
