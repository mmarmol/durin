# Code QA Review — Pass 3 (security / concurrency / error-handling)

**Branch:** `qa/code-review-python` · **Date:** 2026-06-01
**Scope:** angles not covered by pass 1 (bugs/dead-code/tests) or the architecture audit — untrusted-input security, async/thread concurrency & shared state, and error-handling/resource correctness.
**Method:** 3 parallel review agents (python-patterns / python-testing lens), **then every finding re-verified by hand** against the cited code. The "Verified" line on each item is my own confirmation, not the agent's claim.

> Status legend: 🐞 **bug** (fix, no decision) · 🏗 **design** (race needs an approach) · ⚖️ **decision** (product/security posture — operator/founder call). Severity: CRITICAL / HIGH / MED / LOW.

---

## A. Security

### A1 ⚖️ HIGH — Default exec posture is unsandboxed + unrestricted; reachable via prompt injection
- **Where:** `durin/agent/tools/shell.py:38` (`enable=True`), `:41` (`sandbox=""`), `durin/config/schema.py:711` (`restrict_to_workspace=False`).
- **Verified:** all three defaults confirmed by reading. `ExecTool` runs `bash -l -c` (shell.py:284) with no sandbox and no path confinement; the only gate between untrusted chat and exec is channel `allow_from`/pairing (`channels/base.py`). No per-tool human-confirmation gate exists (no confirmation logic in runner/loop).
- **Risk:** an authorized sender gets RCE-as-durin-user by design (personal agent) — but a **prompt-injection** payload in fetched web content / a forwarded message / an ingested doc can instruct the agent to call `exec`. `web_fetch` only prepends a weak "treat as data" banner (`web.py`), which durin's own memory flags as unreliable. The deny-pattern list (`shell.py:105`) blocks ~a handful of patterns (`rm -rf`, fork bombs) and is trivially bypassed (`python -c`, `curl`-exfil, `find -delete`).
- **Not a code bug** — a posture decision. **Question:** make `sandbox=bwrap|docker` + `restrict_to_workspace=True` the default for non-single-user deployments, or document the hardening + state the deny-list is not a security boundary?

### A3 ⚖️ MED — OpenAI-compatible API server has no authentication
- **Where:** `durin/api/server.py:201` (`handle_chat_completions`) — registered by `durin api`.
- **Verified:** read the handler — it pulls `agent_loop`/timeout/model from `request.app` and processes the turn (full agent incl. `exec`); **no token/auth check anywhere in the file** (the 7 "token" hits are LLM-output stream tokens). Unlike the websocket gateway, which enforces `_check_api_token` on every sensitive route, the API server has no auth layer.
- **Risk:** anyone who can reach the port gets unauthenticated, RCE-capable access; `channel="api"` also bypasses the `allow_from` gate (that lives in `BaseChannel`, not the API path).
- **Mitigant:** default bind `127.0.0.1` (schema.py:653), and binding `0.0.0.0` prints a warning. But exposed via reverse proxy / shared host / `0.0.0.0` it is fully open.
- **Question:** add an optional bearer-token check mirroring the websocket gateway's `_check_api_token`?

### A6 ⚖️ LOW — open websocket handshake when token is explicitly disabled
- **Where:** `durin/channels/websocket.py:2115`.
- **Verified:** with `websocket_requires_token=False`, the handshake returns `None` (authorized) regardless of the supplied token. This is an opt-in weakening; default is `requires_token=True`, host `127.0.0.1`. Noted for completeness.

---

## B. Concurrency & shared state

> Reachability fact (verified): `MemoryFileWatcherConfig.enabled` and `MemoryHealthCheckConfig.enabled` both default **True**, and memory is now ON by default. So a default `durin agent` run has the event loop + file-watcher thread + health-check scheduler thread + per-write threshold-Dream daemon threads all touching the same workspace LanceDB table.

### B1 🐞 LOW — `VectorIndex` rebuild is non-atomic (drop-then-create); readers degrade, not crash
- **Where:** `durin/memory/vector_index.py` `rebuild_from_workspace` — `_drop_if_exists(db)` **then** `db.create_table(...)`, a brief no-table window.
- **Re-verified (B1 downgraded HIGH→LOW):** the mechanism is real but the original HIGH risk does not hold.
  - **Docstring did lie** (claimed "dropped only after the new one is built successfully") — **corrected** in the same pass to describe the real drop-then-create + graceful degradation.
  - **No crash.** The *only* `VectorIndex.search()` caller is `_safe_vector_search` (`search_pipeline.py:447-455`), wrapped in `except Exception` → a table-missing error is caught, `search` returns `[]`, and callers fall back to grep. `memory_search` routes through this (`run_search_pipeline`). No unguarded `vi.search()` exists. (The report's own NON-findings already note `_safe_*` degradation is intentional.)
  - **`upsert` window is microscopic** — the embed (`_record_for`) runs *before* the `list_tables` check; the check→`open_table` gap is an `if`. All `reindex_one_file` callers are `except Exception`-guarded (`file_watcher.py:160`, …).
  - **Recovery-only.** The full rebuild fires only when the lance probe already detected breakage (`health_check.py:129` `components["lance"] == "fail"` → `_rebuild_lance`), not on the normal write path.
- **Decision:** docstring fixed; the non-atomic window is **accepted**. A temp-table + `rename_table` swap is feasible (LanceDB 0.30.2 supports it) but its cost — risk on the recovery path, a non-deterministic race test — is not justified for a symptom (transient empty vector results → grep) that is already gracefully degraded and only occurs during an already-broken-index recovery.

---

## Verified NON-findings (checked, ruled out — don't re-investigate)
- **Webhook forgery:** feishu/wecom/qq/dingtalk/weixin/mochat use outbound WebSocket long-connections to vendor servers — no forgeable inbound HTTP surface. msteams validates JWT signing keys; WhatsApp uses a local bridge gated by a random token. `allow_from` enforced centrally in `BaseChannel._handle_message`.
- **Websocket REST auth:** every sensitive handler checks `_check_api_token`; bootstrap/handshake compares use `hmac.compare_digest`; tokens are 32-byte randoms.
- **Path traversal:** filesystem tool routes through `resolve_workspace_path` (`.resolve()` + containment); media writes use uuid + `safe_filename` into a fixed dir.
- **`AliasIndex` race:** already fixed in pass 1 (RLock at `aliases_index.py`).
- **`BaseException` handlers** (runner/loop/memory/cron/session): all re-raise after cleanup or handle `CancelledError` first — none swallow cancellation.
- **search_pipeline `_safe_*` degradation, atomic-write cleanup-and-reraise, untrusted-LLM-JSON broad catch:** intentional and correct.

---

## Suggested order (recommendation, not a commitment)
1. **C1** (close the last warning) + **C2/C3/C4** (silent-failure logging/narrowing — small, clear). 2. **B1** (table-rebuild race + docstring) and **B5** (embedding lock) — real default-config crashes. 3. **B2** (session-rename lost-update). 4. **A2** (SSRF IP-pin). 5. Decisions: **A1** (exec sandbox posture), **A3** (API auth). 6. Remaining design races (**B3/B4/B6**) + LOWs.
