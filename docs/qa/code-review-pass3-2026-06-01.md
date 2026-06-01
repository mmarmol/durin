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

### A2 🐞 HIGH — DNS-rebinding SSRF in `web_fetch` (validate-then-refetch TOCTOU)
- **Where:** `durin/agent/tools/web.py:503` (`_validate_url_safe` → `security/network.py:45 validate_url_target`) then the direct fetches at `web.py:356/380/403` (`httpx.AsyncClient(...).get(url)`).
- **Verified:** `validate_url_target` resolves DNS via `socket.getaddrinfo` and checks the IP against blocked nets (network.py:65). The fetch then calls `client.get(url)` with the **hostname**, and httpx re-resolves independently — there is no custom transport/resolver pinning the validated IP on the direct paths (confirmed: those `async with httpx.AsyncClient(proxy=...)` blocks have no resolver override).
- **Risk:** attacker-controlled DNS returns a public IP at validation time (passes) and `169.254.169.254` / `127.0.0.1` at fetch time → SSRF to cloud metadata / internal services. Reachable from any input that can drive `web_fetch` (authorized sender or prompt injection). The post-fetch `validate_resolved_url` doesn't close it (checks the unchanged hostname, re-resolves a third time).
- **Mitigant:** the *primary* extractor is Jina Reader (external, not SSRF-reachable), but the readability fallback fetches directly.
- **Fix:** resolve once, pin the connection to that IP (custom httpx transport / resolver, or connect-by-IP with a `Host` header).

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

## C. Error handling & resources

### C1 🐞 — `Queue.get` never-awaited warning: root cause is a test that mocks `wait_for`
- **Where:** `tests/agent/tools/test_subagent_tools.py:438` patches `durin.agent.loop.asyncio.wait_for` with `side_effect=asyncio.TimeoutError`; production is `asyncio.wait_for(pending_queue.get(), 300)` (loop.py:1070).
- **Verified:** mocking `wait_for` to raise immediately leaves the already-constructed `pending_queue.get()` coroutine unconsumed → GC'd unawaited → the lone suite warning. Production is fine (real `wait_for` cancels the coroutine). Test artifact only.
- **Fix:** don't patch `wait_for`; patch the timeout constant (or close the passed coroutine before raising). **Closes the last remaining suite warning.**

### C2 🐞 MED — `/plan <task>` swallows a publish failure with no log
- **Where:** `durin/command/builtin.py:879` — `await ...publish_inbound(follow_up)` wrapped in `except Exception: pass` (comment only, no logging). The sibling at `:339` logs via `logger.exception`.
- **Verified:** confirmed silent `pass`. A real bus/dataclass error degrades to "re-type your task" with no diagnostic trail. **Fix:** `logger.exception(...)` before the graceful fallback.

### C3 🐞 MED — `estimate_prompt_tokens` returns 0 on any error (over-broad try)
- **Where:** `durin/utils/helpers.py:480` — `try:` wraps `tiktoken.get_encoding` AND the whole message loop; `except Exception: return 0`.
- **Verified:** confirmed the try spans the dict-access / json.dumps loop, not just the encode. A logic bug (non-dict message, unserializable tool payload) is swallowed → 0 tokens, indistinguishable from an empty prompt, silently corrupting budget/telemetry arithmetic. **Fix:** narrow the try to the `tiktoken` encode call.

### C4 🐞 MED — cron `_compute_next_run` returns `None` on broad except, no log → job silently never fires
- **Where:** `durin/cron/service.py:54` — `except Exception: return None` around croniter, no logging; the cron `expr` isn't validated at add-time.
- **Verified:** confirmed. A malformed cron expression makes the job never schedule, with no warning anywhere — a hard-to-diagnose silent failure in a scheduling subsystem. **Fix:** narrow to expected `croniter`/`ValueError`, `logger.warning` the bad expr, and validate `expr` at add-time.

### C5 🐞 LOW — `gateway_daemon` leaks `log_fd` if `Popen` raises
- **Where:** `durin/cli/gateway_daemon.py:152` (`log_fd = open(...)`) closed at `:164` only on success.
- **Verified:** confirmed — if `subprocess.Popen` (between 152 and 164) raises, the fd leaks. Single-shot spawn, minimal impact. **Fix:** `try/finally`.

### C6 🐞 LOW — over-broad catch on `Path.read_text` readers
- **Where:** `durin/heartbeat/service.py:96` (and similar) catch bare `Exception` around `read_text`, which can only raise `OSError`/`UnicodeDecodeError`. Over-broad; low impact. **Fix:** catch `(OSError, UnicodeDecodeError)` (matches the disciplined pattern in `session/session_meta.py:83`).

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
