# Code QA Review — durin

**Branch:** `qa/code-review-python` · **Date:** 2026-06-01
**Scope:** 247 source files / 84.5K LOC · 402 test files / 104K LOC
**Method:** empirical tooling (ruff E/F/I/N/W + extended B/UP/SIM/RET/PTH/TRY/PERF/RUF, vulture, manual verification) + 3 parallel subsystem review agents, all findings grep/read-verified.
**Lens:** python-patterns + python-testing skills (PEP 8, EAFP, type hints, asyncio task lifetime, pytest discipline).

> Every finding below was verified by reading the code, not inferred from the linter. Registry/dynamic-dispatch false positives (vulture, F821-in-annotations) were filtered out and are listed separately at the end.

---

## P0 — Confirmed runtime bugs — ✅ ALL FIXED (2026-06-01)

Forensic verdict: **all three are genuine bugs, none is a half-finished implementation.** Each was traced to its introducing commit (see below). Fixed with TDD (failing test → fix → green) where the code was testable.

| # | Location | Problem | Forensic verdict | Fix (applied) |
|---|----------|---------|------------------|---------------|
| 1 | [commands.py:1349](../../durin/cli/commands.py#L1349) | `resolve_memory_model(cfg)` — `cfg` undefined; enclosing `on_cron_job` uses `config`. `NameError` whenever the `memory_dream` cron runs. | **Bug — copy-paste var-name error.** Introduced by `70912b4` (aux_models.memory → dream model); the form `resolve_memory_model(cfg)` was copied from `memory_cmd.py` (where the local *is* `cfg`) into a scope where it is `config`. Feature complete + tested at the other 4 call sites; this untested cron path slipped. | `resolve_memory_model(config)`. Verified: F821 clean, module imports. Durable guard: enforce ruff F821 in CI. |
| 2 | [memory/aliases_index.py](../../durin/memory/aliases_index.py) + [aliases_cache.py](../../durin/memory/aliases_cache.py) | `AliasIndex` had **zero internal locking** but is shared process-wide. Search threads `lookup()`/`all_entities()` (iterate `_map`) while the threshold Dream daemon thread `refresh_for()`/`remove()`/`build()` mutates it → `RuntimeError: dictionary changed size during iteration`. | **Bug — active race by default.** In-place mutation was deliberate (§2.C `274d432`, for immediate visibility) and the writer daemon is **on by default** (`dream.enabled=True`, `threshold_entries=5`). Thread-safety was reasoned at the DreamRunner level (one dream runs) but not at the AliasIndex data-structure level. | Added `threading.RLock` guarding lookup/keys/all_entities/size/add/remove/refresh_for; `build()` now populates a fresh map off-lock and swaps it in atomically. RED→GREEN stress test added (`TestThreadSafety`, reliably reproduced the RuntimeError pre-fix). 40/40 alias tests green. |
| 3 | [channels/manager.py:252](../../durin/channels/manager.py#L252) | `asyncio.create_task(...)` fire-and-forget — reference discarded, loop may GC the task before the restart-completion notice sends (RUF006). | **Bug — original oversight.** From the initial commit `73cfb47`, predating the `self._background_tasks` convention later adopted by dingtalk/feishu. Intermittent (gather() usually runs it in time, but not guaranteed). | Added `self._background_tasks` set; task now retained + `add_done_callback(discard)`, mirroring dingtalk. Existing restart test extended to assert tracking + eviction. |

## P1 — Latent traps / quality — ✅ #4–#9 FIXED, #10 → backlog (2026-06-01)

Each investigated forensically (git blame → introducing commit, docs/archive, real code intent) before fixing; behavior changes confirmed before applying.

| # | Location | Problem | Status |
|---|----------|---------|--------|
| 4 | [memory/cross_encoder.py](../../durin/memory/cross_encoder.py) | Public fn `test_model` — the `test_` prefix makes pytest collect it as a test. Suite + callers already aliased it to dodge collection; a future unaliased import becomes a failing "test". | ✅ FIXED — renamed `test_model`→`probe_model` (def + `__all__` + websocket caller + test, dropping the alias workaround). Pure rename, zero behavior change. `36facd0`. |
| 5 | [agent/tools/cron.py:69](../../durin/agent/tools/cron.py#L69) | `ContextVar("cron_metadata", default={})` shared mutable dict (B039). Latent trap, neutralized only incidentally by `channel_meta or {}` in `add_job`. | ✅ FIXED — `default=None` + `get() or {}` at the single read. Every get-site enumerated first. Zero behavior change. `2b0d1bd`. |
| 6 | [agent/tools/message.py:72](../../durin/agent/tools/message.py#L72) | Same B039 default. Already safe (copies on read+write) but tripped the lint. | ✅ FIXED — `default=None` + `dict(get() or {})` at read (user opted to fix both for uniformity; verified behavior-preserving). `2b0d1bd`. |
| 7 | [channels/feishu.py:1468](../../durin/channels/feishu.py#L1468) | B023: lambda captures loop vars `fallback_msg_id`/`card`. Correct today (awaited inline) but a defer-execution refactor would send the last `card` N times. | ✅ FIXED — bound via default args (`c=card, m=fallback_msg_id`), the discord.py idiom. Zero behavior change. `721d062`. |
| 8 | [memory/dream.py:219](../../durin/memory/dream.py#L219) | `text=content` with no None guard; None masks the real cause downstream as a generic parse error. | ✅ FIXED (severity revised down: no crash — `parse_dream_output` already guards `isinstance`). Log at source + coerce `content or ""`. Verified nothing depends on the old behavior. `8815b79`. |
| 9 | [agent/loop.py:1245](../../durin/agent/loop.py#L1245) | `add_done_callback` nested ternary-and lambda — correct but unreadable precedence footgun. | ✅ FIXED — extracted `_drop_active_task(task, key)`; callback delegates. Behavior identical (AST precedence check + 65 lifecycle tests). `06104bf`. |
| 10 | [memory/threshold_trigger.py:161](../../durin/memory/threshold_trigger.py#L161) | `count_pending_for_trigger(workspace)` walks the whole corpus + episodic + entity pages on every qualifying write, uses only a few keys. | ➡️ BACKLOG — the report's suggested fix ("pass `entity_filter`") is **ineffective**: verified the filter is applied post-`load_entry`, so it saves no I/O, and per-entity calls would multiply the walk. Real fix is a structural pending-count index. See [docs/backlog.md](../backlog.md) §2 + `d85886d`. |

## P2 — Dead code (verified zero call sites, incl. dynamic dispatch)

| # | Location | Note |
|---|----------|------|
| 11 | [memory/vector_index.py:300](../../durin/memory/vector_index.py#L300) | `compose_embedding_text` classmethod — never called; docstring admits it exists only to satisfy a doc-promise. Callers use `_compose_entity_page_text`/`_embed_text` directly. Delete, or route the two callers through it. |
| 12 | [memory/search.py:451](../../durin/memory/search.py#L451) | `_all_classes_iter()` — never referenced. Delete. |
| 13 | [agent/tools/memory_search.py:752](../../durin/agent/tools/memory_search.py#L752) | `_vector_row_to_result` — orphaned; live path uses `_sectioned_to_result` (L682). Delete. |
| 14 | [agent/tools/search.py:71](../../durin/agent/tools/search.py#L71) | `_pagination_note` — never called (companion `_paginate` is used). Delete or wire in. |
| 15 | [providers/github_copilot_provider.py:57](../../durin/providers/github_copilot_provider.py#L57) | `get_github_copilot_login_status()` — zero refs across durin/tests/webui. Delete or wire into OAuth status. |
| — | [memory/absorption.py:187](../../durin/memory/absorption.py#L187), [memory/graph.py:77](../../durin/memory/graph.py#L77), [agent/tools/memory_ingest.py:202](../../durin/agent/tools/memory_ingest.py#L202) | F841 unused locals (`archived_path`, `episodic_root`, `ingested_id`). Remove. |

## P3 — Test gaps & hygiene

| # | Location | Problem | Fix |
|---|----------|---------|-----|
| 16 | [pyproject.toml:143](../../pyproject.toml#L143) | `@pytest.mark.slow` used (test_dream.py:501) but `slow` **not registered** → `pytest -m "not slow"` does NOT exclude it; `--strict-markers` would error. | Register `slow` in `markers`. |
| 17 | tests/memory/test_aliases_cache.py | Only concurrency test covers cache *construction*; nothing exercises `lookup()` + `refresh_for()`/`build()` on the shared instance — exactly the P0#2 race. | Add reader/writer stress test. |
| 18 | [agent/tools/memory_search.py — tests](../../tests/agent/test_stop_preserves_context.py#L49) | `test_restore_checkpoint_method_exists` / `test_checkpoint_key_constant` assert only `hasattr`/string equality — pass even if logic is broken. Behavioral coverage already exists adjacently. | Drop the two tautological tests. |
| 19 | [channels/mochat.py](../../durin/channels/mochat.py) (943 LOC) | **No test file** (`grep -i mochat tests/` empty) while sibling channels each have suites. Non-trivial buffering/delay/mention logic untested. | Add tests/channels/test_mochat_channel.py. |

## Bulk lint (mechanical, `ruff --fix`)

`ruff check durin/` (current config): **124 errors, 83 auto-fixable** — 49 unsorted imports (I001), 29 unused imports (F401), 21 E402, 5 empty f-strings (F541). Extended ruleset surfaces **1239** more (UP/SIM/RET/PTH/PERF/TRY) if the team wants to adopt them. Recommend running `ruff check durin/ --fix` for the safe 83, then reviewing the 4 F821-in-annotations below.

## Filtered as NON-issues (don't re-investigate)

- **4× F821 in annotations** — `Optional` (onboard_memory.py:102-103), `Path` (builtin.py:1317,1502), `Any` (dream.py:99). All modules have `from __future__ import annotations`, so these are never-evaluated string annotations → **no runtime bug**. Latent only (would break `get_type_hints()`). Add the missing imports if you care about introspection.
- **~200 vulture 60%-confidence hits** — almost all false positives: Tool subclasses (`ExecTool`, `WebSearchTool`, …) instantiate via registry; channel classes load dynamically; `loop.py:_state_*` methods dispatch via `getattr(f"_state_{name}")` (loop.py:1557). Verified non-dead.
- **~13 `except Exception: pass` in memory/** — best-effort telemetry emitters, correctly `# pragma: no cover`. Intended contract.
- **FallbackProvider, atomic-write `except BaseException`, subprocess reaping** — all checked, correct.

---

### Suggested order
1. P0 #1 (`cfg`) — one-char fix, prevents a NameError crash. 2. P0 #2 (AliasIndex lock) + #17 test. 3. P0 #3 (dangling task). 4. `ruff --fix` the 83 mechanical. 5. P1 traps #4–#10. 6. P2 dead-code deletions. 7. P3 remaining test gaps.
