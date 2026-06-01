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

### B1 🐞 HIGH — `VectorIndex` rebuild drops-then-creates the table (docstring lies); concurrent readers crash
- **Where:** `durin/memory/vector_index.py:524` (`_drop_if_exists(db)` **then** `db.create_table(...)`). The docstring at `:442-443` claims *"the existing table is dropped only after the new one is built successfully."*
- **Verified:** the code does drop-then-create — the opposite of the docstring. There is a real window with no table. `search` (`:569`) does `if _TABLE_NAME not in db.list_tables(): return []`, then spends ~tens of ms in `embed_query`, then `open_table` — if a rebuild thread drops the table in that gap, `open_table` raises table-not-found. `upsert`/`upsert_entity_page` have the same TOCTOU.
- **Risk:** unhandled exception reachable in default config (the health-check/Dream threads rebuild/reindex concurrently with loop reads). `memory_search` catches `VectorIndexDimensionMismatch`, not arbitrary table-missing errors.
- **Fix:** build the new table under a temp name then swap, or serialize rebuilds with a lock and wrap `search`/`upsert` table access in a tolerant retry. Also correct the false docstring.

### B2 🐞 MED — Session rename lost-update: operates on a `_load` snapshot, not the cached `Session`
- **Where:** `durin/channels/websocket.py:2047` (`_handle_session_rename` calls `self._session_manager._load(decoded_key)` — fresh disk read, bypasses `_cache`), mutates `metadata`, `save`s.
- **Verified:** read both paths. The agent loop holds the **cached** `Session` (via `get_or_create`) and appends messages during a turn, then `save`s. `save` rewrites the whole `.jsonl` (last-writer-wins). The two paths hold different `Session` objects → rename's save loses in-flight messages, or the loop's save loses the title.
- **Fix:** rename/delete should mutate the cached instance (`get_or_create`) so both share one object. Same class of bug for any out-of-band `_load`+`save`.

### B3 🏗 MED — `_dispatch` registers its pending-queue inside the task, racing the inbound consumer
- **Where:** `durin/agent/loop.py:1229` (consumer: `if effective_key in self._pending_queues`) vs `:1259` (`create_task(self._dispatch(msg))`) and `:1276` (`self._pending_queues[session_key] = pending`, inside the task body).
- **Verified:** registration happens inside the dispatch task, which runs after `create_task` yields back to the consumer. A second same-session message consumed in that window finds the key unregistered → spawns a **second** dispatch task; the per-session lock serializes processing (no concurrency/corruption), but the second registration overwrites the first and follow-up routing / mid-turn injection is mis-ordered for that window.
- **Approach:** register the pending queue synchronously before `create_task` (or via `setdefault` keyed on the effective session key) so the consumer sees it immediately.

### B4 🏗 MED — Shared `FastembedProvider` used by the loop and the Dream daemon thread concurrently
- **Where:** `durin/agent/tools/memory_store.py:328` passes the store tool's cached `vi` (one `FastembedProvider`) into `_maybe_dispatch_threshold_dream` → `threshold_trigger.py:205` → `DreamRunner.run` on a `threading.Thread`.
- **Verified:** confirmed the same provider instance is reachable from the loop (`memory_store.execute`) and the daemon thread simultaneously. fastembed/ONNX `embed` isn't guaranteed thread-safe on one session, and lazy `if self._model is None: self._load()` (embedding.py:304) is check-then-act.
- **Approach:** give the daemon path its own provider/index, or guard embed with a lock. (Inference is mostly stateless so corruption is unlikely; the real exposure is the first-use `_load`/registration window — see B5.)

### B5 🐞 MED — Unguarded module globals in `embedding.py` (`_REGISTERED_CUSTOM`, `_CATALOG_CACHE`)
- **Where:** `durin/memory/embedding.py:124` (mutates `_REGISTERED_CUSTOM`), `:156` (assigns `_CATALOG_CACHE`), no lock; on the embed hot path via `_load` → `_register_custom_models`.
- **Verified:** confirmed unguarded (this is the same global pair behind the order-dependent test bug fixed in pass 1). Two threads can both pass the `model_id in _REGISTERED_CUSTOM` check and both call `TextEmbedding.add_custom_model`, which raises `ValueError` on re-registration — caught only for `ImportError`, so it propagates out of provider init.
- **Fix:** a module lock around register + catalog-cache assignment (mirror the `aliases_cache` pattern).

### B6 🏗 MED — `VectorIndex.upsert` delete+add is non-atomic
- **Where:** `durin/memory/vector_index.py:143-144` (`table.delete(...)` then `table.add([record])` — two LanceDB commits).
- **Verified:** confirmed two separate commits. A concurrent reader can observe the row deleted-but-not-yet-re-added (a recently-written memory momentarily vanishing from search); the health-check thread reaches this via `_repair_drift → reindex_one_file` on common mtime-lag drift. No permanent corruption (Lance per-commit atomic), but transient missing rows during heal ticks.

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
